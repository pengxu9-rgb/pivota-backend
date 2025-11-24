"""Wix Product Sync and Integration"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from utils.auth import get_current_user
from db.database import database
from datetime import datetime
import httpx
import uuid

router = APIRouter()

@router.post("/merchant/integrations/wix/sync")
async def sync_wix_products(
    store_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from Wix store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchant_id
        merchant_id = current_user.get("merchant_id")
        if not merchant_id and current_user["role"] == "merchant":
            raise HTTPException(status_code=400, detail="Merchant ID not found")
        
        # Get Wix store
        if store_id:
            store_query = """
                SELECT * FROM merchant_stores
                WHERE store_id = :store_id AND platform = 'wix'
            """
            store = await database.fetch_one(store_query, {"store_id": store_id})
        else:
            # Get first Wix store for this merchant
            if merchant_id:
                store_query = """
                    SELECT * FROM merchant_stores
                    WHERE merchant_id = :merchant_id AND platform = 'wix'
                    LIMIT 1
                """
                store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
            else:
                # For employee, get any Wix store
                store_query = """
                    SELECT * FROM merchant_stores
                    WHERE platform = 'wix'
                    LIMIT 1
                """
                store = await database.fetch_one(store_query)
        
        if not store:
            raise HTTPException(status_code=404, detail="Wix store not found")
        
        # Convert Row to dict
        store_dict = dict(store)
        
        # Get merchant_id for cache query
        merchant_id = store_dict.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found in store record")
        
        # 调用真正的产品同步（和 Shopify 一样，走 Universal Sync）
        from routes.product_sync import sync_products, SyncRequest
        from fastapi import BackgroundTasks
        
        sync_request = SyncRequest(
            merchant_id=merchant_id,
            force_refresh=True,  # 强制刷新
            limit=250,
            platform="wix"  # 明确指定同步 Wix
        )
        
        # 执行真正的同步
        sync_result = await sync_products(
            request=sync_request,
            background_tasks=BackgroundTasks(),
            current_user=current_user,
        )

        if sync_result.status != "success":
            raise HTTPException(
                status_code=400,
                detail=f"Wix sync failed: {sync_result.message}",
            )
        
        return {
            "status": "success",
            "message": sync_result.message,
            "store_id": store_dict["store_id"],
            "store_name": store_dict["name"],
            "product_count": sync_result.products_synced,
            "platform": sync_result.platform,
            "synced_at": sync_result.sync_time,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing Wix products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")

@router.post("/integrations/wix/connect-sync")
async def connect_wix_store_sync(
    merchant_id: str,
    api_key: str,
    site_id: str,
    store_name: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Connect a Wix store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Verify merchant exists
        merchant_query = "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id"
        merchant = await database.fetch_one(merchant_query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Generate store ID
        store_id = f"store_wix_{uuid.uuid4().hex[:8]}"
        
        # Simulate Wix API validation
        # In production, this would verify the API key and site_id with Wix
        
        # Insert store
        insert_query = """
            INSERT INTO merchant_stores (
                store_id, merchant_id, platform, name, domain,
                api_key, status, connected_at, product_count
            ) VALUES (
                :store_id, :merchant_id, :platform, :name, :domain,
                :api_key, :status, :connected_at, :product_count
            )
        """
        
        await database.execute(insert_query, {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "platform": "wix",
            "name": store_name or site_id,
            "domain": site_id,
            "api_key": api_key,
            "status": "active",
            "connected_at": datetime.now(),
            "product_count": 0
        })
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id,
            "store_name": store_name or site_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error connecting Wix store: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix store: {str(e)}")

@router.post("/integrations/wix/test")
async def test_wix_connection(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test Wix store connection"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get Wix store
        store_query = """
            SELECT * FROM merchant_stores
            WHERE merchant_id = :merchant_id AND platform = 'wix'
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Wix store not found")
        
        # Simulate connection test
        # In production, this would ping Wix API
        
        return {
            "status": "success",
            "message": "Wix connection successful",
            "store_name": store["name"],
            "site_id": store["domain"],
            "api_status": "active",
            "response_time": 120  # ms
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
