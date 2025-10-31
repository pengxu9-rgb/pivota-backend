"""Sync products from ALL connected platforms"""
from services.merchant_store_service import get_merchant_active_stores
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import List, Dict, Any
from pydantic import BaseModel
from utils.auth import get_current_user
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from datetime import datetime
import json
import logging
from adapters.product_adapters import fetch_merchant_products
from db.products import upsert_product_cache
from routes.universal_product_sync import prepare_platform_credentials, update_sync_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products", tags=["product-sync-all"])

class SyncAllRequest(BaseModel):
    merchant_id: str
    force_refresh: bool = True
    limit: int = 250

class PlatformSyncResult(BaseModel):
    platform: str
    status: str
    message: str
    products_synced: int

class SyncAllResponse(BaseModel):
    status: str
    message: str
    merchant_id: str
    platforms_synced: List[PlatformSyncResult]
    total_products: int
    sync_time: str

@router.post("/sync-all-platforms/", response_model=SyncAllResponse)
async def sync_all_platforms(
    request: SyncAllRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Sync products from ALL connected platforms for a merchant.
    Unlike sync-universal which only syncs the first platform found.
    """
    start_time = datetime.now()
    results = []
    total_synced = 0
    
    try:
        logger.info(f"🔄 Sync ALL platforms request: merchant_id={request.merchant_id}")
        
        # Get merchant info
        merchant = await get_merchant_onboarding(request.merchant_id)
        if not merchant:
            return SyncAllResponse(
                status="error",
                message="Merchant account not found",
                merchant_id=request.merchant_id,
                platforms_synced=[],
                total_products=0,
                sync_time=datetime.now().isoformat()
            )
        
        # Get ALL connected stores
        stores = await get_merchant_active_stores(request.merchant_id)
        
        if not stores:
            return SyncAllResponse(
                status="success",
                message="No stores connected. Please connect your stores in Integrations.",
                merchant_id=request.merchant_id,
                platforms_synced=[],
                total_products=0,
                sync_time=datetime.now().isoformat()
            )
        
        logger.info(f"Found {len(stores)} connected stores for merchant {request.merchant_id}")
        
        # Sync each platform
        for store in stores:
            platform = store["platform"]
            logger.info(f"Syncing {platform} store: {store.get('name', store['store_id'])}")
            
            try:
                # Prepare credentials
                credentials = prepare_platform_credentials(platform, store)
                
                if not credentials:
                    results.append(PlatformSyncResult(
                        platform=platform,
                        status="warning",
                        message=f"Missing or incomplete credentials for {platform}",
                        products_synced=0
                    ))
                    continue
                
                # Fetch products
                products_obj, _, error = await fetch_merchant_products(
                    merchant_id=request.merchant_id,
                    platform=platform,
                    credentials=credentials,
                    limit=request.limit
                )
                
                if error:
                    results.append(PlatformSyncResult(
                        platform=platform,
                        status="error", 
                        message=f"API error: {str(error)}",
                        products_synced=0
                    ))
                    continue
                
                # Cache products
                synced_count = 0
                if products_obj:
                    for product in products_obj:
                        try:
                            product_data = json.loads(product.json())
                            await upsert_product_cache(
                                merchant_id=request.merchant_id,
                                platform=platform,
                                platform_product_id=product.id,
                                product_data=product_data,
                                ttl_seconds=604800  # 7 days
                            )
                            synced_count += 1
                        except Exception as e:
                            logger.error(f"Failed to cache product {product.id}: {e}")
                
                results.append(PlatformSyncResult(
                    platform=platform,
                    status="success",
                    message=f"Successfully synced {synced_count} products",
                    products_synced=synced_count
                ))
                total_synced += synced_count
                
                # Update store sync status
                await update_sync_status(store.get("store_id"), synced_count)
                
            except Exception as e:
                logger.error(f"Error syncing {platform}: {e}")
                results.append(PlatformSyncResult(
                    platform=platform,
                    status="error",
                    message=f"Unexpected error: {str(e)}",
                    products_synced=0
                ))
        
        # Determine overall status
        success_count = sum(1 for r in results if r.status == "success")
        error_count = sum(1 for r in results if r.status == "error")
        
        if success_count == len(results):
            overall_status = "success"
            overall_message = f"Successfully synced {total_synced} products from {len(results)} platforms"
        elif error_count == len(results):
            overall_status = "error"
            overall_message = "Failed to sync products from all platforms"
        else:
            overall_status = "partial"
            overall_message = f"Synced {total_synced} products from {success_count}/{len(results)} platforms"
        
        return SyncAllResponse(
            status=overall_status,
            message=overall_message,
            merchant_id=request.merchant_id,
            platforms_synced=results,
            total_products=total_synced,
            sync_time=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Unexpected error in multi-platform sync: {e}")
        return SyncAllResponse(
            status="error",
            message=f"Sync service error: {str(e)}",
            merchant_id=request.merchant_id,
            platforms_synced=[],
            total_products=0,
            sync_time=datetime.now().isoformat()
        )


