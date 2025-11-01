"""Initialize merchant data for production"""
from fastapi import APIRouter
from db.database import database
from datetime import datetime

router = APIRouter()

@router.post("/init-merchant-data")
@router.get("/init-merchant-data")  # Also allow GET for easy testing
async def init_merchant_data():
    """Initialize merchant data with real Shopify and PSP connections"""
    
    merchant_id = "merch_6b90dc9838d5fd9c"
    
    try:
        # Check if merchant exists
        check_query = "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id"
        merchant_exists = await database.fetch_one(check_query, {"merchant_id": merchant_id})
        
        if not merchant_exists:
            # Create merchant
            await database.execute("""
                INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email, store_url, status, mcp_connected, mcp_platform, mcp_shop_domain, psp_connected, psp_type)
                VALUES (:merchant_id, :business_name, :contact_email, :store_url, :status, :mcp_connected, :mcp_platform, :mcp_shop_domain, :psp_connected, :psp_type)
            """, {
                "merchant_id": merchant_id,
                "business_name": "ChydanTest Store",
                "contact_email": "merchant@test.com",
                "store_url": "https://chydantest.myshopify.com",
                "status": "approved",
                "mcp_connected": True,
                "mcp_platform": "shopify",
                "mcp_shop_domain": "chydantest.myshopify.com",
                "psp_connected": True,
                "psp_type": "stripe"
            })
        else:
            # Update merchant
            await database.execute("""
                UPDATE merchant_onboarding 
                SET store_url = :store_url,
                    mcp_connected = :mcp_connected,
                    mcp_platform = :mcp_platform,
                    mcp_shop_domain = :mcp_shop_domain,
                    psp_connected = :psp_connected,
                    psp_type = :psp_type
                WHERE merchant_id = :merchant_id
            """, {
                "merchant_id": merchant_id,
                "store_url": "https://chydantest.myshopify.com",
                "mcp_connected": True,
                "mcp_platform": "shopify",
                "mcp_shop_domain": "chydantest.myshopify.com",
                "psp_connected": True,
                "psp_type": "stripe"
            })
        
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

