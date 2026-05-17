"""Direct database check endpoint - bypass all business logic"""
from fastapi import APIRouter, Depends
from db.database import database
from routes.auth_routes import require_admin
from utils.runtime_safety import require_runtime_gate
import datetime

router = APIRouter()

@router.get("/version-check")
async def version_check():
    """Check deployed version and configuration"""
    return {
        "version": "v1.0.1-fixed-db-import",
        "timestamp": datetime.datetime.now().isoformat(),
        "database_module": str(type(database)),
        "has_execute": hasattr(database, 'execute'),
        "has_fetch_all": hasattr(database, 'fetch_all'),
        "has_fetch_one": hasattr(database, 'fetch_one')
    }

@router.get("/direct-db-check")
async def direct_db_check(current_user: dict = Depends(require_admin)):
    """Directly query database without any auth or filtering"""
    require_runtime_gate("ENABLE_DIRECT_DB_CHECK")
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
        merchant_id = str(current_user.get("merchant_id") or "").strip()
        if merchant_id:
            stores_query = "SELECT * FROM merchant_stores WHERE merchant_id = :merchant_id"
            stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
            result["current_merchant_stores"] = [dict(s) for s in stores]
            result["current_merchant_stores_count"] = len(stores)

            psps_query = "SELECT * FROM merchant_psps WHERE merchant_id = :merchant_id"
            psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
            result["current_merchant_psps"] = [dict(p) for p in psps]
            result["current_merchant_psps_count"] = len(psps)
    except Exception as e:
        result["current_merchant_error"] = str(e)
    
    return result
