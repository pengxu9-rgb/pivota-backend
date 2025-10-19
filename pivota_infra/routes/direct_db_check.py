"""Direct database check endpoint - bypass all business logic"""
from fastapi import APIRouter
from db.database import database

router = APIRouter()

@router.get("/direct-db-check")
async def direct_db_check():
    """Directly query database without any auth or filtering"""
    result = {}
    
    try:
        # Check all stores
        stores_query = "SELECT * FROM merchant_stores ORDER BY connected_at DESC LIMIT 20"
        stores = await database.fetch_all(stores_query)
        result["all_stores"] = [dict(s) for s in stores]
        result["stores_count"] = len(stores)
    except Exception as e:
        result["stores_error"] = str(e)
    
    try:
        # Check all PSPs
        psps_query = "SELECT * FROM merchant_psps ORDER BY connected_at DESC LIMIT 20"
        psps = await database.fetch_all(psps_query)
        result["all_psps"] = [dict(p) for p in psps]
        result["psps_count"] = len(psps)
    except Exception as e:
        result["psps_error"] = str(e)
    
    try:
        # Check specific merchant
        merchant_id = "merch_6b90dc9838d5fd9c"
        stores_query = "SELECT * FROM merchant_stores WHERE merchant_id = :merchant_id"
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        result["test_merchant_stores"] = [dict(s) for s in stores]
        result["test_merchant_stores_count"] = len(stores)
        
        psps_query = "SELECT * FROM merchant_psps WHERE merchant_id = :merchant_id"
        psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        result["test_merchant_psps"] = [dict(p) for p in psps]
        result["test_merchant_psps_count"] = len(psps)
    except Exception as e:
        result["test_merchant_error"] = str(e)
    
    return result

