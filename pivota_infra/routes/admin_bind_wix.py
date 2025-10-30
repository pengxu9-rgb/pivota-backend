from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.database import database
from utils.auth import get_current_user
from datetime import datetime
import secrets
import logging

router = APIRouter(prefix="/admin/bind", tags=["admin-bind"])
logger = logging.getLogger(__name__)

class BindWixStoreRequest(BaseModel):
    merchant_id: str
    site_id: str
    api_key: str
    store_url: str

@router.post("/wix-store")
async def admin_bind_wix_store(
    request: BindWixStoreRequest,
    current_user: dict = Depends(get_current_user)
):
    """Admin can bind a Wix store to any merchant (bypass auth check)"""
    if current_user["role"] not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'wix' AND domain = :site_id""",
            {"merchant_id": request.merchant_id, "site_id": request.site_id}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :token, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "token": request.api_key}
            )
            store_id = existing_store["store_id"]
            message = "Wix store updated successfully"
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'wix', :site_id, :name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "site_id": request.site_id,
                    "name": f"Wix Store ({request.store_url})",
                    "token": request.api_key
                }
            )
            message = "Wix store connected successfully"
        
        logger.info(f"Admin bound Wix store for merchant {request.merchant_id}")
        
        return {
            "status": "success",
            "message": message,
            "store_id": store_id,
            "merchant_id": request.merchant_id,
            "site_id": request.site_id
        }
        
    except Exception as e:
        logger.error(f"Error binding Wix store: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

