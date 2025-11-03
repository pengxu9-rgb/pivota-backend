import asyncio
import os
from databases import Database

async def check_merchant():
    # Use Railway database URL
    database_url = "postgresql://postgres:HJXUswMlQvvYvebIWYaJqrqOtMKZmaPg@junction.proxy.rlwy.net:47708/railway"
    database = Database(database_url)
    
    try:
        await database.connect()
        print("🔍 Checking merchant@test.com data...")
        print("=" * 80)
        
        # Find merchant
        merchant = await database.fetch_one(
            "SELECT merchant_id, business_name, email FROM merchant_onboarding WHERE email = 'merchant@test.com'"
        )
        
        if not merchant:
            print("❌ Merchant not found!")
            return
        
        merchant_id = merchant['merchant_id']
        print(f"\n✅ Found merchant:")
        print(f"   ID: {merchant_id}")
        print(f"   Business: {merchant['business_name']}")
        print(f"   Email: {merchant['email']}")
        
        # Check orders
        print(f"\n📦 Checking orders for {merchant_id}...")
        orders = await database.fetch_all(
            """
            SELECT id, order_id, total_amount, payment_status, psp_type, 
                   shopify_order_id, created_at
            FROM orders 
            WHERE merchant_id = :merchant_id
            ORDER BY created_at DESC
            LIMIT 10
            """,
            {"merchant_id": merchant_id}
        )
        
        if orders:
            print(f"   Found {len(orders)} orders:")
            for order in orders:
                print(f"   - {order['order_id']}: ${order['total_amount']/100:.2f} via {order['psp_type']} - {order['payment_status']}")
                if order['shopify_order_id']:
                    print(f"     Shopify: {order['shopify_order_id']}")
        else:
            print("   ⚠️  No orders found!")
        
        # Check PSP configurations
        print(f"\n💳 Checking PSP configurations...")
        psps = await database.fetch_all(
            """
            SELECT provider, status, connected_at, LENGTH(api_key) as key_length
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            """,
            {"merchant_id": merchant_id}
        )
        
        if psps:
            print(f"   Found {len(psps)} PSP configs:")
            for psp in psps:
                print(f"   - {psp['provider']}: {psp['status']} (key length: {psp['key_length']})")
        else:
            print("   ⚠️  No PSP configurations found!")
        
        # Check Shopify connection
        print(f"\n🛍️  Checking Shopify connection...")
        shopify = await database.fetch_one(
            """
            SELECT mcp_platform, mcp_connected, mcp_shop_domain, 
                   LENGTH(mcp_access_token) as token_length
            FROM merchant_onboarding
            WHERE merchant_id = :merchant_id
            """,
            {"merchant_id": merchant_id}
        )
        
        if shopify and shopify['mcp_connected']:
            print(f"   ✅ Shopify connected:")
            print(f"   Platform: {shopify['mcp_platform']}")
            print(f"   Shop: {shopify['mcp_shop_domain']}")
            print(f"   Token: {'Yes' if shopify['token_length'] else 'No'}")
        else:
            print("   ⚠️  Shopify not connected!")
        
        # Check user account
        print(f"\n👤 Checking user account...")
        user = await database.fetch_one(
            "SELECT id, email, role FROM users WHERE email = 'merchant@test.com'"
        )
        
        if user:
            print(f"   ✅ User exists: {user['email']} (role: {user['role']})")
        else:
            print("   ⚠️  User account not found!")
        
        print("\n" + "=" * 80)
        
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(check_merchant())
