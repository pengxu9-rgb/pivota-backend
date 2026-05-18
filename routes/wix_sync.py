"""Wix Product Sync and Integration"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from utils.auth import get_current_user
from db.database import database
from datetime import datetime
import uuid
from services.wix_connection import (
    WixConnectionValidationError,
    validate_wix_catalog_access,
)

router = APIRouter()

_PLATFORM_LABELS = {
    "wix": "Wix",
    "woocommerce": "WooCommerce",
    "bigcommerce": "BigCommerce",
}


async def _sync_connected_platform_products(
    *,
    platform: str,
    store_id: Optional[str],
    current_user: dict,
):
    platform_label = _PLATFORM_LABELS.get(platform, platform.title())

    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id and current_user["role"] == "merchant":
        raise HTTPException(status_code=400, detail="Merchant ID not found")

    if store_id:
        store_query = """
            SELECT * FROM merchant_stores
            WHERE store_id = :store_id
              AND platform = :platform
              AND status IN ('active', 'connected')
        """
        store = await database.fetch_one(
            store_query,
            {"store_id": store_id, "platform": platform},
        )
    elif merchant_id:
        store_query = """
            SELECT * FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND status IN ('active', 'connected')
            LIMIT 1
        """
        store = await database.fetch_one(
            store_query,
            {"merchant_id": merchant_id, "platform": platform},
        )
    else:
        raise HTTPException(status_code=400, detail="store_id or merchant context is required")

    if not store:
        raise HTTPException(status_code=404, detail=f"{platform_label} store not found")

    store_dict = dict(store)
    merchant_id = store_dict.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in store record")

    from routes.product_sync import sync_products, SyncRequest
    from fastapi import BackgroundTasks

    sync_request = SyncRequest(
        merchant_id=merchant_id,
        force_refresh=True,
        limit=250,
        platform=platform,
    )

    sync_result = await sync_products(
        request=sync_request,
        background_tasks=BackgroundTasks(),
        current_user=current_user,
    )

    if sync_result.status != "success":
        raise HTTPException(
            status_code=400,
            detail=f"{platform_label} sync failed: {sync_result.message}",
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


@router.post("/merchant/integrations/wix/sync")
async def sync_wix_products(
    store_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from Wix store"""
    try:
        return await _sync_connected_platform_products(
            platform="wix",
            store_id=store_id,
            current_user=current_user,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing Wix products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.post("/merchant/integrations/woocommerce/sync")
async def sync_woocommerce_products(
    store_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from WooCommerce store"""
    try:
        return await _sync_connected_platform_products(
            platform="woocommerce",
            store_id=store_id,
            current_user=current_user,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing WooCommerce products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.post("/merchant/integrations/bigcommerce/sync")
async def sync_bigcommerce_products(
    store_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from BigCommerce store"""
    try:
        return await _sync_connected_platform_products(
            platform="bigcommerce",
            store_id=store_id,
            current_user=current_user,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing BigCommerce products: {e}")
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
        
        try:
            validation = await validate_wix_catalog_access(site_id, api_key)
        except WixConnectionValidationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            )

        normalized_site_id = validation["site_id"]
        normalized_api_key = validation["api_key"]

        existing = await database.fetch_one(
            """
            SELECT store_id
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = 'wix'
              AND domain = :domain
            """,
            {"merchant_id": merchant_id, "domain": normalized_site_id},
        )

        if existing:
            store_id = existing["store_id"]
            await database.execute(
                """
                UPDATE merchant_stores
                SET name = :name,
                    api_key = :api_key,
                    status = 'active',
                    connected_at = :connected_at
                WHERE store_id = :store_id
                """,
                {
                    "store_id": store_id,
                    "name": store_name or normalized_site_id,
                    "api_key": normalized_api_key,
                    "connected_at": datetime.now(),
                },
            )
        else:
            store_id = f"store_wix_{uuid.uuid4().hex[:8]}"
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
                "name": store_name or normalized_site_id,
                "domain": normalized_site_id,
                "api_key": normalized_api_key,
                "status": "active",
                "connected_at": datetime.now(),
                "product_count": 0
            })
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id,
            "store_name": store_name or normalized_site_id
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
              AND status IN ('active', 'connected')
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Wix store not found")
        
        try:
            validation = await validate_wix_catalog_access(store["domain"], store["api_key"])
        except WixConnectionValidationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            )
        
        return {
            "status": "success",
            "message": "Wix connection successful",
            "store_name": store["name"],
            "site_id": validation["site_id"],
            "api_status": "active",
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
