"""Clean up test data and keep only real integrations"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import get_current_user
from utils.runtime_safety import require_runtime_gate

router = APIRouter()

@router.post("/cleanup-test-data")
@router.get("/cleanup-test-data")
async def cleanup_test_data(current_user: dict = Depends(get_current_user)):
    """Remove all test integrations, keep only real ones"""
    require_runtime_gate("ENABLE_TEST_DATA_CLEANUP")
    merchant_id = str(current_user.get("merchant_id") or "").strip()
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required in current user context")
    
    try:
        # Delete all stores except the real Shopify one
        delete_test_stores = """
            DELETE FROM merchant_stores 
            WHERE merchant_id = :merchant_id 
            AND lower(coalesce(status, '')) NOT IN ('active', 'connected')
        """
        result1 = await database.execute(delete_test_stores, {"merchant_id": merchant_id})
        
        # Delete all PSPs except the real Stripe one
        delete_test_psps = """
            DELETE FROM merchant_psps 
            WHERE merchant_id = :merchant_id 
            AND lower(coalesce(status, '')) NOT IN ('active', 'connected')
        """
        result2 = await database.execute(delete_test_psps, {"merchant_id": merchant_id})
        
        # Verify remaining integrations
        stores_query = "SELECT store_id, platform, name FROM merchant_stores WHERE merchant_id = :merchant_id"
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        
        psps_query = "SELECT psp_id, provider, name FROM merchant_psps WHERE merchant_id = :merchant_id"
        psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "Test data cleaned up successfully",
            "remaining": {
                "stores": [{"id": s["store_id"], "platform": s["platform"], "name": s["name"]} for s in stores],
                "psps": [{"id": p["psp_id"], "provider": p["provider"], "name": p["name"]} for p in psps]
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to cleanup: {str(e)}"
        }






