"""
Admin Shopify health check endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from pydantic import BaseModel
from typing import Optional
from db.database import database
import json
import httpx
import logging
import os

logger = logging.getLogger(__name__)
# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# GET /admin/shopify/health/{merchant_id} anonymously read merchant_stores
# (domain, token presence) and made outbound Shopify calls on the way.
router = APIRouter(prefix="/admin/shopify", tags=["admin-shopify-health"], dependencies=[Depends(require_admin_or_key)])

class ShopifyHealthResponse(BaseModel):
    merchant_id: str
    domain: Optional[str]
    token_present: bool
    storefront_token_present: bool = False
    storefront_reachable: Optional[bool] = None
    storefront_http_status: Optional[int] = None
    reachable: bool
    http_status: Optional[int]
    error: Optional[str]
    hint: Optional[str]

async def _get_shopify_store(merchant_id: str):
    row = await database.fetch_one(
        """
        SELECT domain, api_key, status
        FROM merchant_stores
        WHERE merchant_id = :m AND platform = 'shopify'
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"m": merchant_id},
    )
    return row

@router.get("/health/{merchant_id}", response_model=ShopifyHealthResponse)
async def shopify_health(merchant_id: str):
    try:
        store = await _get_shopify_store(merchant_id)
        if not store:
            return ShopifyHealthResponse(
                merchant_id=merchant_id,
                domain=None,
                token_present=False,
                storefront_token_present=bool(
                    (os.getenv("SHOPIFY_STOREFRONT_ACCESS_TOKEN", "") or "").strip()
                    and (os.getenv("SHOPIFY_STOREFRONT_ALLOW_GLOBAL_TOKEN", "") or "").strip().lower()
                    in {"1", "true", "yes", "on"}
                ),
                storefront_reachable=None,
                storefront_http_status=None,
                reachable=False,
                http_status=404,
                error="No Shopify store connected",
                hint="Connect Shopify in Integrations first",
            )
        domain = store["domain"]
        api_key_raw = store["api_key"] or ""

        token = None
        storefront_token = None
        if api_key_raw:
            try:
                data = json.loads(api_key_raw)
                token = data.get("access_token") or data.get("token") or data.get("shopify_access_token")
                storefront_token = (
                    data.get("storefront_access_token")
                    or data.get("storefront_token")
                    or data.get("storefrontAccessToken")
                )
            except Exception:
                token = api_key_raw

        if not storefront_token:
            allow_global = (os.getenv("SHOPIFY_STOREFRONT_ALLOW_GLOBAL_TOKEN", "") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if allow_global:
                env_token = (os.getenv("SHOPIFY_STOREFRONT_ACCESS_TOKEN", "") or "").strip() or None
                env_domain = (os.getenv("SHOPIFY_STOREFRONT_ACCESS_TOKEN_DOMAIN", "") or "").strip().lower()
                if env_token and env_domain and (domain or "").strip().lower() != env_domain:
                    env_token = None
                storefront_token = env_token

        if not domain:
            return ShopifyHealthResponse(
                merchant_id=merchant_id,
                domain=None,
                token_present=bool(token),
                storefront_token_present=bool(storefront_token),
                storefront_reachable=None,
                storefront_http_status=None,
                reachable=False,
                http_status=400,
                error="Shopify domain missing",
                hint="Ensure domain is like 'yourstore.myshopify.com'",
            )

        # Best-effort Storefront API ping (does not reveal token).
        storefront_reachable = None
        storefront_http_status = None
        if storefront_token:
            try:
                sf_url = f"https://{domain}/api/2025-10/graphql.json"
                sf_headers = {
                    "X-Shopify-Storefront-Access-Token": storefront_token,
                    "Content-Type": "application/json",
                }
                sf_payload = {"query": "query { shop { name } }"}
                async with httpx.AsyncClient(timeout=8) as client:
                    sf_resp = await client.post(sf_url, headers=sf_headers, json=sf_payload)
                storefront_http_status = sf_resp.status_code
                storefront_reachable = sf_resp.status_code == 200
            except Exception:
                storefront_reachable = False

        headers = {}
        if token:
            headers["X-Shopify-Access-Token"] = token
        url = f"https://{domain}/admin/api/2025-10/shop.json"

        http_status = None
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=headers)
                http_status = resp.status_code
                if resp.status_code == 200:
                    return ShopifyHealthResponse(
                        merchant_id=merchant_id,
                        domain=domain,
                        token_present=bool(token),
                        storefront_token_present=bool(storefront_token),
                        storefront_reachable=storefront_reachable,
                        storefront_http_status=storefront_http_status,
                        reachable=True,
                        http_status=resp.status_code,
                        error=None,
                        hint="OK",
                    )
                else:
                    return ShopifyHealthResponse(
                        merchant_id=merchant_id,
                        domain=domain,
                        token_present=bool(token),
                        storefront_token_present=bool(storefront_token),
                        storefront_reachable=storefront_reachable,
                        storefront_http_status=storefront_http_status,
                        reachable=False,
                        http_status=resp.status_code,
                        error=resp.text[:300],
                        hint=(
                            "401/403: token invalid or missing; 404: domain wrong; 5xx: Shopify side issue"
                        ),
                    )
        except httpx.RequestError as e:
            return ShopifyHealthResponse(
                merchant_id=merchant_id,
                domain=domain,
                token_present=bool(token),
                storefront_token_present=bool(storefront_token),
                storefront_reachable=storefront_reachable,
                storefront_http_status=storefront_http_status,
                reachable=False,
                http_status=http_status,
                error=str(e),
                hint="Network/DNS/SSL issue. Check domain and token",
            )
    except Exception as e:
        logger.error(f"Shopify health failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

