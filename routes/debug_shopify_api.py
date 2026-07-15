"""
Debug endpoint to test Shopify API directly
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
from utils.logger import logger
from db.database import database
from services.shopify_access_token_service import resolve_shopify_admin_access_token

router = APIRouter(prefix="/debug", tags=["debug"])

class ShopifyTestResponse(BaseModel):
    status: str
    shop_domain: str
    api_response_code: int
    product_count: int
    raw_response: dict
    error: str = None

@router.get("/test-shopify/{merchant_id}")
async def test_shopify_api(merchant_id: str):
    """直接测试 Shopify API 调用"""
    try:
        # 1. 获取 Shopify 商店信息
        store = await database.fetch_one(
            """
            SELECT store_id, domain, api_key, status, connected_at
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = 'shopify'
              AND status IN ('active', 'connected')
            ORDER BY connected_at DESC NULLS LAST
            LIMIT 1
            """,
            {"merchant_id": merchant_id}
        )
        
        if not store:
            raise HTTPException(status_code=404, detail="Shopify store not found")
        
        shop_domain = store["domain"]
        api_key_raw = store["api_key"]
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=api_key_raw,
            store_id=str(store.get("store_id") or "").strip() or None,
        )
        if not access_token:
            raise HTTPException(status_code=400, detail="Shopify access token missing/invalid (stored api_key)")
        
        logger.info(f"🔍 Testing Shopify API: domain={shop_domain}, has_token={bool(access_token)}")
        
        # 2. 直接调用 Shopify API
        url = f"https://{shop_domain}/admin/api/2025-10/products.json"
        headers = {"X-Shopify-Access-Token": access_token}
        params = {"limit": 250}
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
        
        logger.info(f"📊 Shopify API response: status={response.status_code}")
        
        # 3. 解析响应
        if response.status_code != 200:
            return {
                "status": "error",
                "shop_domain": shop_domain,
                "api_response_code": response.status_code,
                "product_count": 0,
                "raw_response": {},
                "error": response.text[:500]
            }
        
        data = response.json()
        products = data.get("products", [])
        
        return {
            "status": "success",
            "shop_domain": shop_domain,
            "api_response_code": response.status_code,
            "product_count": len(products),
            "raw_response": {
                "products_sample": products[:2] if products else [],
                "total_in_response": len(products)
            },
            "error": None
        }
        
    except Exception as e:
        logger.error(f"❌ Shopify API test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
