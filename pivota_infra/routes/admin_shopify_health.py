"""
Admin Shopify health check endpoints
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from db.database import database
import json
import httpx
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/shopify", tags=["admin-shopify-health"])

class ShopifyHealthResponse(BaseModel):
    merchant_id: str
    domain: Optional[str]
    token_present: bool
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
                reachable=False,
                http_status=404,
                error="No Shopify store connected",
                hint="Connect Shopify in Integrations first",
            )
        domain = store["domain"]
        api_key_raw = store["api_key"] or ""

        token = None
        if api_key_raw:
            try:
                data = json.loads(api_key_raw)
                token = data.get("access_token") or data.get("token") or data.get("shopify_access_token")
            except Exception:
                token = api_key_raw

        if not domain:
            return ShopifyHealthResponse(
                merchant_id=merchant_id,
                domain=None,
                token_present=bool(token),
                reachable=False,
                http_status=400,
                error="Shopify domain missing",
                hint="Ensure domain is like 'yourstore.myshopify.com'",
            )

        headers = {}
        if token:
            headers["X-Shopify-Access-Token"] = token
        url = f"https://{domain}/admin/api/2023-10/shop.json"

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
                reachable=False,
                http_status=http_status,
                error=str(e),
                hint="Network/DNS/SSL issue. Check domain and token",
            )
    except Exception as e:
        logger.error(f"Shopify health failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
