"""Cleanup all duplicate stores and PSPs"""
from fastapi import APIRouter
from db.database import database

router = APIRouter()

@router.post("/cleanup-all-duplicates")
@router.get("/cleanup-all-duplicates")
async def cleanup_all_duplicates():
    """Remove all duplicates, keep only one of each platform/provider"""
    
    merchant_id = "merch_6b90dc9838d5fd9c"
    
    result = {}
    
    try:
        # Delete all stores first
        await database.execute("DELETE FROM merchant_stores WHERE merchant_id = :merchant_id", {"merchant_id": merchant_id})
        
        # Re-insert only the real ones
        await database.execute("""
            INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, status, product_count, connected_at)
            VALUES 
            (:store_id1, :merchant_id, 'shopify', 'chydantest.myshopify.com', 'chydantest.myshopify.com', 'connected', 4, NOW()),
            (:store_id2, :merchant_id, 'wix', 'peng652.wixsite.com/aydan-1', 'peng652.wixsite.com/aydan-1', 'connected', 0, NOW())
        """, {
            "store_id1": "store_shopify_chydantest",
            "store_id2": "store_wix_peng652",
            "merchant_id": merchant_id
        })
        
        result["stores_cleaned"] = "success"
        
        # Delete all PSPs first
        await database.execute("DELETE FROM merchant_psps WHERE merchant_id = :merchant_id", {"merchant_id": merchant_id})
        
        # Re-insert only the real ones
        await database.execute("""
            INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, account_id, capabilities, status, connected_at)
            VALUES 
            (:psp_id1, :merchant_id, 'stripe', 'Stripe Account', 'acct_real_stripe', 'card,bank_transfer,alipay,wechat_pay', 'active', NOW()),
            (:psp_id2, :merchant_id, 'adyen', 'Adyen Account', 'acct_real_adyen', 'card,bank_transfer', 'active', NOW())
        """, {
            "psp_id1": "psp_stripe_main",
            "psp_id2": "psp_adyen_main",
            "merchant_id": merchant_id
        })
        
        result["psps_cleaned"] = "success"
        
        # Verify
        stores = await database.fetch_all("SELECT store_id, platform, name FROM merchant_stores WHERE merchant_id = :merchant_id", {"merchant_id": merchant_id})
        psps = await database.fetch_all("SELECT psp_id, provider, name FROM merchant_psps WHERE merchant_id = :merchant_id", {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "All duplicates cleaned up",
            "final_state": {
                "stores": [{"id": s["store_id"], "platform": s["platform"], "name": s["name"]} for s in stores],
                "psps": [{"id": p["psp_id"], "provider": p["provider"], "name": p["name"]} for p in psps]
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "result": result
        }





