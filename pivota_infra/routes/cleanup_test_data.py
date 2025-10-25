"""Clean up test data and keep only real integrations"""
from fastapi import APIRouter, Depends
from db.database import database
from utils.auth import get_current_user

router = APIRouter()

@router.post("/cleanup-test-data")
@router.get("/cleanup-test-data")
async def cleanup_test_data(current_user: dict = Depends(get_current_user)):
    """Remove all test integrations, keep only real ones"""
    
    merchant_id = "merch_6b90dc9838d5fd9c"
    
    try:
        # Delete all stores except the real Shopify one
        delete_test_stores = """
            DELETE FROM merchant_stores 
            WHERE merchant_id = :merchant_id 
            AND store_id != 'store_shopify_chydantest'
        """
        result1 = await database.execute(delete_test_stores, {"merchant_id": merchant_id})
        
        # Delete all PSPs except the real Stripe one
        delete_test_psps = """
            DELETE FROM merchant_psps 
            WHERE merchant_id = :merchant_id 
            AND psp_id != 'psp_stripe_chydantest'
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






