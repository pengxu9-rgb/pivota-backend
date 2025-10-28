"""
Merchant Store Connections
Allow merchants to connect their own stores (Shopify, Wix, etc.)
"""
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from typing import Dict, Any, Optional
import logging
import httpx
from datetime import datetime

from db.database import database
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Merchant Integrations"])


class ConnectShopifyRequest(BaseModel):
    merchant_id: str
    shop_domain: str
    access_token: str


class ConnectWixRequest(BaseModel):
    merchant_id: str
    site_id: str
    api_key: str
    store_name: Optional[str] = None


@router.post("/shopify/connect")
async def merchant_connect_shopify(
    request: ConnectShopifyRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Shopify store"""
    # Allow merchant, employee, or admin
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # If merchant role, verify they can only connect their own store
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        # Validate shop domain and access token
        if not request.shop_domain or not request.shop_domain.strip():
            raise HTTPException(status_code=400, detail="Shop domain is required")
        
        if not request.access_token or not request.access_token.strip():
            raise HTTPException(status_code=400, detail="Access token is required")
        
        # Test Shopify API connection
        test_url = f"https://{request.shop_domain}/admin/api/2024-07/shop.json"
        headers = {"X-Shopify-Access-Token": request.access_token}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            test_response = await client.get(test_url, headers=headers)
        
        if test_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid Shopify credentials. API returned: {test_response.status_code}"
            )
        
        # Verify shop data
        shop_data = test_response.json()
        if not shop_data.get("shop"):
            raise HTTPException(status_code=400, detail="Invalid Shopify response")
        
        shop_info = shop_data["shop"]
        logger.info(f"✅ Shopify credentials verified for {request.shop_domain}")
        
        # Check if store already exists
        existing = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'shopify' 
               AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": request.shop_domain}
        )
        
        if existing:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET access_token = :token, 
                       status = 'active',
                       updated_at = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"token": request.access_token, "store_id": existing["store_id"]}
            )
            store_id = existing["store_id"]
        else:
            # Create new store record
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, store_name, access_token, status, created_at)
                   VALUES (:store_id, :merchant_id, 'shopify', :domain, :store_name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": request.shop_domain,
                    "store_name": shop_info.get("name", request.shop_domain),
                    "token": request.access_token
                }
            )
        
        # Also update merchant_onboarding MCP fields
        await database.execute(
            """UPDATE merchant_onboarding 
               SET mcp_connected = true,
                   mcp_platform = 'shopify',
                   mcp_shop_domain = :domain,
                   mcp_access_token = :token,
                   updated_at = CURRENT_TIMESTAMP
               WHERE merchant_id = :merchant_id""",
            {
                "merchant_id": request.merchant_id,
                "domain": request.shop_domain,
                "token": request.access_token
            }
        )
        
        return {
            "status": "success",
            "message": "Shopify store connected successfully",
            "store_id": store_id,
            "shop_name": shop_info.get("name"),
            "shop_domain": request.shop_domain
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting Shopify: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect Shopify: {str(e)}")


@router.post("/wix/connect")
async def merchant_connect_wix(
    request: ConnectWixRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Wix store"""
    # Allow merchant, employee, or admin
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # If merchant role, verify they can only connect their own store
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        # Validate inputs
        if not request.site_id or not request.site_id.strip():
            raise HTTPException(status_code=400, detail="Wix Site ID is required")
        
        if not request.api_key or not request.api_key.strip():
            raise HTTPException(status_code=400, detail="Wix API Key is required")
        
        # Test Wix API connection (simplified check)
        test_url = "https://www.wixapis.com/stores/v1/products/query"
        headers = {
            "Authorization": request.api_key,
            "wix-site-id": request.site_id
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            test_response = await client.post(
                test_url,
                json={"query": {"limit": 1}},
                headers=headers
            )
        
        if test_response.status_code not in [200, 401, 403]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Wix credentials. API returned: {test_response.status_code}"
            )
        
        logger.info(f"✅ Wix credentials verified for site {request.site_id}")
        
        # Store in merchant_stores table
        store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
        
        await database.execute(
            """INSERT INTO merchant_stores 
               (store_id, merchant_id, platform, domain, store_name, access_token, status, created_at)
               VALUES (:store_id, :merchant_id, 'wix', :site_id, :store_name, :token, 'active', CURRENT_TIMESTAMP)
               ON CONFLICT (merchant_id, platform, domain) 
               DO UPDATE SET access_token = EXCLUDED.access_token, status = 'active', updated_at = CURRENT_TIMESTAMP""",
            {
                "store_id": store_id,
                "merchant_id": request.merchant_id,
                "site_id": request.site_id,
                "store_name": request.store_name or f"Wix Store {request.site_id[:8]}",
                "token": request.api_key
            }
        )
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting Wix: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix: {str(e)}")

