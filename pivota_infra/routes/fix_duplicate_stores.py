"""Fix duplicate stores - keep only unique ones"""
from fastapi import APIRouter, Depends
from db.database import database
from utils.auth import get_current_user

router = APIRouter()

@router.post("/fix-duplicate-stores")
@router.get("/fix-duplicate-stores")
async def fix_duplicate_stores(current_user: dict = Depends(get_current_user)):
    """Remove duplicate stores, keep the newest one for each platform/domain"""
    
    merchant_id = "merch_6b90dc9838d5fd9c"
    
    try:
        # Delete duplicate Shopify store (keep the one created by main.py)
        await database.execute(
            "DELETE FROM merchant_stores WHERE store_id = 'store_shopify_real' AND merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # Verify
        stores_query = "SELECT store_id, platform, name FROM merchant_stores WHERE merchant_id = :merchant_id ORDER BY platform, name"
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "Duplicate stores removed",
            "remaining_stores": [{"id": s["store_id"], "platform": s["platform"], "name": s["name"]} for s in stores]
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


