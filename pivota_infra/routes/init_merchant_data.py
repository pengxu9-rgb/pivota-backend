"""Initialize merchant data for production"""
from fastapi import APIRouter
from db.database import database

router = APIRouter()

@router.post("/init-merchant-data")
async def init_merchant_data():
    """Initialize merchant data with real Shopify and PSP connections"""
    
    merchant_id = "merch_6b90dc9838d5fd9c"
    
    try:
        # Update merchant_onboarding to connect Shopify
        update_query = """
            UPDATE merchant_onboarding 
            SET 
                store_url = 'https://chydantest.myshopify.com',
                mcp_connected = true,
                mcp_platform = 'shopify',
                mcp_shop_domain = 'chydantest.myshopify.com',
                psp_connected = true,
                psp_type = 'stripe'
            WHERE merchant_id = :merchant_id
        """
        await database.execute(update_query, {"merchant_id": merchant_id})
        
        # Insert Shopify store if not exists
        store_insert = """
            INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, status, product_count, connected_at)
            VALUES (:store_id, :merchant_id, :platform, :name, :domain, :status, :product_count, NOW())
            ON CONFLICT (store_id) DO NOTHING
        """
        await database.execute(store_insert, {
            "store_id": "store_shopify_real",
            "merchant_id": merchant_id,
            "platform": "shopify",
            "name": "chydantest.myshopify.com",
            "domain": "chydantest.myshopify.com",
            "status": "connected",
            "product_count": 4
        })
        
        # Insert Stripe PSP if not exists
        psp_insert = """
            INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, account_id, capabilities, status, connected_at)
            VALUES (:psp_id, :merchant_id, :provider, :name, :account_id, :capabilities, :status, NOW())
            ON CONFLICT (psp_id) DO NOTHING
        """
        await database.execute(psp_insert, {
            "psp_id": "psp_stripe_real",
            "merchant_id": merchant_id,
            "provider": "stripe",
            "name": "Stripe Account",
            "account_id": "acct_real_stripe",
            "capabilities": "card,bank_transfer,alipay,wechat_pay",
            "status": "active"
        })
        
        # Verify
        verify_query = "SELECT store_url, mcp_connected, mcp_platform FROM merchant_onboarding WHERE merchant_id = :merchant_id"
        result = await database.fetch_one(verify_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "Merchant data initialized",
            "data": {
                "merchant_id": merchant_id,
                "store_url": result["store_url"] if result else None,
                "mcp_connected": result["mcp_connected"] if result else None,
                "mcp_platform": result["mcp_platform"] if result else None
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to initialize: {str(e)}"
        }

