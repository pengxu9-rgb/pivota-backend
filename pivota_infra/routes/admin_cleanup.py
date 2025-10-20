"""Admin cleanup - no auth required for quick fixes"""
from fastapi import APIRouter
from db.database import database

router = APIRouter()

@router.get("/admin/cleanup-duplicates")
async def admin_cleanup_duplicates():
    """Admin endpoint to cleanup duplicates - no auth required for emergency fixes"""
    
    merchant_id = "merch_6b90dc9838d5fd9c"
    
    try:
        # Get all current data first
        all_stores_before = await database.fetch_all("SELECT store_id FROM merchant_stores WHERE merchant_id = :m", {"m": merchant_id})
        all_psps_before = await database.fetch_all("SELECT psp_id FROM merchant_psps WHERE merchant_id = :m", {"m": merchant_id})
        
        # Delete ALL stores
        await database.execute("DELETE FROM merchant_stores WHERE merchant_id = :m", {"m": merchant_id})
        
        # Delete ALL PSPs  
        await database.execute("DELETE FROM merchant_psps WHERE merchant_id = :m", {"m": merchant_id})
        
        # Insert clean data - Shopify
        await database.execute("""
            INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, status, product_count, connected_at)
            VALUES ('store_shopify_main', :m, 'shopify', 'chydantest.myshopify.com', 'chydantest.myshopify.com', 'connected', 4, NOW())
        """, {"m": merchant_id})
        
        # Insert clean data - Wix
        await database.execute("""
            INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, status, product_count, connected_at)
            VALUES ('store_wix_main', :m, 'wix', 'peng652.wixsite.com/aydan-1', 'peng652.wixsite.com/aydan-1', 'connected', 0, NOW())
        """, {"m": merchant_id})
        
        # Insert clean data - Stripe
        await database.execute("""
            INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, account_id, capabilities, status, connected_at)
            VALUES ('psp_stripe_main', :m, 'stripe', 'Stripe Account', 'acct_real', 'card,bank_transfer,alipay,wechat_pay', 'active', NOW())
        """, {"m": merchant_id})
        
        # Insert clean data - Adyen
        await database.execute("""
            INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, account_id, capabilities, status, connected_at)
            VALUES ('psp_adyen_main', :m, 'adyen', 'Adyen Account', 'acct_adyen', 'card,bank_transfer', 'active', NOW())
        """, {"m": merchant_id})
        
        # Verify final state
        stores_after = await database.fetch_all("SELECT store_id, platform, name FROM merchant_stores WHERE merchant_id = :m", {"m": merchant_id})
        psps_after = await database.fetch_all("SELECT psp_id, provider, name FROM merchant_psps WHERE merchant_id = :m", {"m": merchant_id})
        
        return {
            "status": "success",
            "message": "Cleanup completed",
            "before": {
                "stores": len(all_stores_before),
                "psps": len(all_psps_before)
            },
            "after": {
                "stores": [{"id": s["store_id"], "platform": s["platform"], "name": s["name"]} for s in stores_after],
                "psps": [{"id": p["psp_id"], "provider": p["provider"], "name": p["name"]} for p in psps_after]
            }
        }
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc()
        }

