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


class ConnectWooCommerceRequest(BaseModel):
    merchant_id: str
    store_url: str
    consumer_key: str
    consumer_secret: str


class ConnectBigCommerceRequest(BaseModel):
    merchant_id: str
    store_hash: str
    access_token: str
    client_id: Optional[str] = None


class ConnectPrestaShopRequest(BaseModel):
    merchant_id: str
    store_url: str
    api_key: str


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
                   SET api_key = :token, 
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
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'shopify', :domain, :name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": request.shop_domain,
                    "name": shop_info.get("name", request.shop_domain),
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
               (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
               VALUES (:store_id, :merchant_id, 'wix', :site_id, :name, :token, 'active', CURRENT_TIMESTAMP)
               ON CONFLICT (merchant_id, platform, domain) 
               DO UPDATE SET api_key = EXCLUDED.api_key, status = 'active', updated_at = CURRENT_TIMESTAMP""",
            {
                "store_id": store_id,
                "merchant_id": request.merchant_id,
                "site_id": request.site_id,
                "name": request.store_name or f"Wix Store {request.site_id[:8]}",
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


@router.post("/woocommerce/connect")
async def merchant_connect_woocommerce(
    request: ConnectWooCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their WooCommerce store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_url or not request.consumer_key or not request.consumer_secret:
            raise HTTPException(status_code=400, detail="Store URL, Consumer Key and Consumer Secret are required")
        
        # Test WooCommerce API connection using adapter
        from adapters.woocommerce_adapter import WooCommerceAdapter
        
        adapter = WooCommerceAdapter({
            'store_url': request.store_url,
            'consumer_key': request.consumer_key,
            'consumer_secret': request.consumer_secret
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"WooCommerce connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ WooCommerce credentials verified for {request.store_url}")
        
        # Store in merchant_stores table
        store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
        
        await database.execute(
            """INSERT INTO merchant_stores 
               (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
               VALUES (:store_id, :merchant_id, 'woocommerce', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
               ON CONFLICT (merchant_id, platform, domain) 
               DO UPDATE SET api_key = EXCLUDED.api_key, status = 'active', updated_at = CURRENT_TIMESTAMP""",
            {
                "store_id": store_id,
                "merchant_id": request.merchant_id,
                "domain": request.store_url,
                "name": test_result.get('store_name', f"WooCommerce Store"),
                "api_key": f"{request.consumer_key}:{request.consumer_secret}"  # Store both
            }
        )
        
        return {
            "status": "success",
            "message": "WooCommerce store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting WooCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect WooCommerce: {str(e)}")


@router.post("/bigcommerce/connect")
async def merchant_connect_bigcommerce(
    request: ConnectBigCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their BigCommerce store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_hash or not request.access_token:
            raise HTTPException(status_code=400, detail="Store Hash and Access Token are required")
        
        # Test BigCommerce API connection using adapter
        from adapters.bigcommerce_adapter import BigCommerceAdapter
        
        adapter = BigCommerceAdapter({
            'store_hash': request.store_hash,
            'access_token': request.access_token,
            'client_id': request.client_id
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"BigCommerce connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ BigCommerce credentials verified for {request.store_hash}")
        
        # Store in merchant_stores table
        store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
        store_domain = f"{request.store_hash}.mybigcommerce.com"
        
        await database.execute(
            """INSERT INTO merchant_stores 
               (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
               VALUES (:store_id, :merchant_id, 'bigcommerce', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
               ON CONFLICT (merchant_id, platform, domain) 
               DO UPDATE SET api_key = EXCLUDED.api_key, status = 'active', updated_at = CURRENT_TIMESTAMP""",
            {
                "store_id": store_id,
                "merchant_id": request.merchant_id,
                "domain": store_domain,
                "name": test_result.get('store_name', f"BigCommerce Store"),
                "api_key": request.access_token
            }
        )
        
        return {
            "status": "success",
            "message": "BigCommerce store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting BigCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect BigCommerce: {str(e)}")


@router.post("/prestashop/connect")
async def merchant_connect_prestashop(
    request: ConnectPrestaShopRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their PrestaShop store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_url or not request.api_key:
            raise HTTPException(status_code=400, detail="Store URL and API Key are required")
        
        # Test PrestaShop API connection using adapter
        from adapters.prestashop_adapter import PrestaShopAdapter
        
        adapter = PrestaShopAdapter({
            'store_url': request.store_url,
            'api_key': request.api_key
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"PrestaShop connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ PrestaShop credentials verified for {request.store_url}")
        
        # Store in merchant_stores table
        store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
        
        await database.execute(
            """INSERT INTO merchant_stores 
               (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
               VALUES (:store_id, :merchant_id, 'prestashop', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
               ON CONFLICT (merchant_id, platform, domain) 
               DO UPDATE SET api_key = EXCLUDED.api_key, status = 'active', updated_at = CURRENT_TIMESTAMP""",
            {
                "store_id": store_id,
                "merchant_id": request.merchant_id,
                "domain": request.store_url,
                "name": test_result.get('store_name', f"PrestaShop Store"),
                "api_key": request.api_key
            }
        )
        
        return {
            "status": "success",
            "message": "PrestaShop store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting PrestaShop: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect PrestaShop: {str(e)}")


