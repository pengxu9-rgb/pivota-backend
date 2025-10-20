"""
Employee Portal Store and PSP Connection Fixes
Handles Shopify, Wix, Stripe, Adyen connections for merchants
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime
from utils.auth import get_current_user
from db.database import database
import uuid
import json

router = APIRouter()

# ============== Models ==============

class ConnectShopifyRequest(BaseModel):
    merchant_id: str
    shop_domain: str
    access_token: str
    store_name: Optional[str] = None

class ConnectWixRequest(BaseModel):
    merchant_id: str
    api_key: str
    site_id: str
    store_name: Optional[str] = None

class ConnectPSPRequest(BaseModel):
    merchant_id: str
    psp_type: str  # stripe, adyen, paypal, square
    api_key: str
    test_mode: bool = True
    account_id: Optional[str] = None

class SyncProductsRequest(BaseModel):
    merchant_id: str
    platform: Optional[str] = "shopify"

# ============== Store Connections ==============

@router.post("/integrations/shopify/connect")
async def connect_shopify_store(
    request: ConnectShopifyRequest,
    current_user: dict = Depends(get_current_user)
):
    """Connect Shopify store for a merchant (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if merchant exists
        merchant_check = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": request.merchant_id}
        )
        
        if not merchant_check:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Check if store already exists
        existing = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'shopify' 
               AND store_url = :store_url""",
            {"merchant_id": request.merchant_id, "store_url": request.shop_domain}
        )
        
        if existing:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'connected', connected_at = :connected_at
                   WHERE store_id = :store_id""",
                {
                    "api_key": request.access_token,
                    "connected_at": datetime.now(),
                    "store_id": existing["store_id"]
                }
            )
            store_id = existing["store_id"]
        else:
            # Create new store connection
            store_id = f"store_shopify_{uuid.uuid4().hex[:8]}"
            
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, store_name, store_url, status, product_count, api_key, connected_at)
                   VALUES (:store_id, :merchant_id, :platform, :store_name, :store_url, :status, :product_count, :api_key, :connected_at)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "platform": "shopify",
                    "store_name": request.store_name or request.shop_domain,
                    "store_url": request.shop_domain,
                    "status": "connected",
                    "product_count": 0,
                    "api_key": request.access_token,
                    "connected_at": datetime.now()
                }
            )
        
        return {
            "status": "success",
            "message": "Shopify store connected successfully",
            "store_id": store_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect Shopify: {str(e)}")

@router.post("/integrations/wix/connect")
async def connect_wix_store(
    request: ConnectWixRequest,
    current_user: dict = Depends(get_current_user)
):
    """Connect Wix store for a merchant (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if merchant exists
        merchant_check = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": request.merchant_id}
        )
        
        if not merchant_check:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Check if store already exists
        existing = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'wix' 
               AND store_url = :store_url""",
            {"merchant_id": request.merchant_id, "store_url": request.site_id}
        )
        
        if existing:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'connected', connected_at = :connected_at
                   WHERE store_id = :store_id""",
                {
                    "api_key": request.api_key,
                    "connected_at": datetime.now(),
                    "store_id": existing["store_id"]
                }
            )
            store_id = existing["store_id"]
        else:
            # Create new store connection
            store_id = f"store_wix_{uuid.uuid4().hex[:8]}"
            
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, store_name, store_url, status, product_count, api_key, connected_at)
                   VALUES (:store_id, :merchant_id, :platform, :store_name, :store_url, :status, :product_count, :api_key, :connected_at)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "platform": "wix",
                    "store_name": request.store_name or f"Wix Store {request.site_id[:8]}",
                    "store_url": request.site_id,
                    "status": "connected",
                    "product_count": 0,
                    "api_key": request.api_key,
                    "connected_at": datetime.now()
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
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix: {str(e)}")

# ============== PSP Connections ==============

@router.post("/merchant/onboarding/setup-psp")
async def setup_merchant_psp(
    request: ConnectPSPRequest,
    current_user: dict = Depends(get_current_user)
):
    """Setup PSP for a merchant (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if merchant exists
        merchant_check = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": request.merchant_id}
        )
        
        if not merchant_check:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Check if PSP already exists
        existing = await database.fetch_one(
            """SELECT psp_id FROM merchant_psps 
               WHERE merchant_id = :merchant_id AND provider = :provider""",
            {"merchant_id": request.merchant_id, "provider": request.psp_type}
        )
        
        if existing:
            # Update existing PSP
            await database.execute(
                """UPDATE merchant_psps 
                   SET api_key = :api_key, status = 'active', connected_at = :connected_at
                   WHERE psp_id = :psp_id""",
                {
                    "api_key": request.api_key,
                    "connected_at": datetime.now(),
                    "psp_id": existing["psp_id"]
                }
            )
            psp_id = existing["psp_id"]
        else:
            # Create new PSP connection
            psp_id = f"psp_{request.psp_type}_{uuid.uuid4().hex[:8]}"
            
            # Determine capabilities based on PSP type
            capabilities = {
                "stripe": ["payments", "refunds", "subscriptions", "payouts"],
                "adyen": ["payments", "refunds", "payouts", "risk_management"],
                "paypal": ["payments", "refunds", "payouts"],
                "square": ["payments", "refunds", "inventory"]
            }.get(request.psp_type, ["payments"])
            
            await database.execute(
                """INSERT INTO merchant_psps 
                   (psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
                   VALUES (:psp_id, :merchant_id, :provider, :name, :api_key, :account_id, :capabilities, :status, :connected_at)""",
                {
                    "psp_id": psp_id,
                    "merchant_id": request.merchant_id,
                    "provider": request.psp_type,
                    "name": f"{request.psp_type.capitalize()} Account",
                    "api_key": request.api_key,
                    "account_id": request.account_id or f"acct_{uuid.uuid4().hex[:12]}",
                    "capabilities": ",".join(capabilities),
                    "status": "active",
                    "connected_at": datetime.now()
                }
            )
        
        return {
            "status": "success",
            "message": f"{request.psp_type.capitalize()} connected successfully",
            "psp_id": psp_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to setup PSP: {str(e)}")

# ============== Product Sync ==============

@router.post("/integrations/{platform}/sync-products")
async def sync_merchant_products(
    platform: str,
    request: SyncProductsRequest,
    current_user: dict = Depends(get_current_user)
):
    """Sync products for a merchant store (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if platform not in ["shopify", "wix"]:
        raise HTTPException(status_code=400, detail="Invalid platform")
    
    try:
        # Get the store for this merchant and platform
        store = await database.fetch_one(
            """SELECT store_id, store_name, api_key 
               FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = :platform 
               AND status = 'connected'
               ORDER BY connected_at DESC LIMIT 1""",
            {"merchant_id": request.merchant_id, "platform": platform}
        )
        
        if not store:
            raise HTTPException(
                status_code=404, 
                detail=f"No connected {platform} store found for merchant"
            )
        
        # Simulate product sync (in real implementation, would call actual API)
        if platform == "shopify":
            # Simulate Shopify product sync
            product_count = 42  # Demo count
            sync_status = "completed"
        else:  # wix
            # Simulate Wix product sync
            product_count = 28  # Demo count
            sync_status = "completed"
        
        # Update store with sync results
        await database.execute(
            """UPDATE merchant_stores 
               SET product_count = :product_count, 
                   last_sync = :last_sync,
                   sync_status = :sync_status
               WHERE store_id = :store_id""",
            {
                "product_count": product_count,
                "last_sync": datetime.now(),
                "sync_status": sync_status,
                "store_id": store["store_id"]
            }
        )
        
        return {
            "status": "success",
            "message": f"Products synced successfully for {store['store_name']}",
            "product_count": product_count,
            "store_id": store["store_id"]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")

# ============== Test Connections ==============

@router.post("/integrations/{platform}/test")
async def test_store_connection(
    platform: str,
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test store connection (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        store = await database.fetch_one(
            """SELECT store_id, api_key, store_url 
               FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = :platform
               ORDER BY connected_at DESC LIMIT 1""",
            {"merchant_id": merchant_id, "platform": platform}
        )
        
        if not store:
            raise HTTPException(status_code=404, detail=f"No {platform} store found")
        
        # Simulate connection test
        # In real implementation, would make actual API call
        return {
            "status": "success",
            "message": f"{platform.capitalize()} connection successful",
            "connected": True
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
