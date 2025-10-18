"""
Setup Shopify for merchant and test order creation
"""
import os
import sys
import asyncio
import json
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, 'pivota_infra')

# Database imports
from db.database import database, metadata
from db.merchant_onboarding import merchant_onboarding
from db.orders import orders
from config.settings import settings

MERCHANT_ID = 'merch_208139f7600dbf42'
SHOPIFY_STORE = 'chydantest.myshopify.com'
SHOPIFY_TOKEN = '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'

async def update_merchant_shopify():
    """Update merchant with Shopify credentials"""
    print("🔧 Updating merchant with Shopify credentials...")
    print("="*70)
    
    # Connect to database
    await database.connect()
    
    try:
        # Update merchant with Shopify fields
        query = merchant_onboarding.update().where(
            merchant_onboarding.c.merchant_id == MERCHANT_ID
        ).values(
            mcp_connected=True,
            mcp_platform='shopify',
            mcp_shop_domain=SHOPIFY_STORE,
            mcp_access_token=SHOPIFY_TOKEN,
            updated_at=datetime.now()
        )
        
        result = await database.execute(query)
        print(f"✅ Updated merchant record (rows affected: {result})")
        
        # Verify the update
        select_query = merchant_onboarding.select().where(
            merchant_onboarding.c.merchant_id == MERCHANT_ID
        )
        merchant = await database.fetch_one(select_query)
        
        if merchant:
            print(f"\nVerification:")
            print(f"  mcp_connected: {merchant['mcp_connected']}")
            print(f"  mcp_platform: {merchant['mcp_platform']}")
            print(f"  mcp_shop_domain: {merchant['mcp_shop_domain']}")
            print(f"  Has token: {bool(merchant['mcp_access_token'])}")
            
        # Now trigger Shopify order creation for paid orders
        print("\n📦 Checking for paid orders without Shopify ID...")
        
        orders_query = orders.select().where(
            (orders.c.payment_status == 'paid') &
            (orders.c.shopify_order_id.is_(None))
        ).order_by(orders.c.created_at.desc())
        
        paid_orders = await database.fetch_all(orders_query)
        
        if paid_orders:
            order = paid_orders[0]
            print(f"\nFound paid order: {order['order_id']}")
            print(f"Customer: {order['customer_email']}")
            print(f"Total: ${order['total']}")
            print(f"Created: {order['created_at']}")
            
            # Import the Shopify order creation function
            from routes.order_routes import create_shopify_order
            
            print("\n🚀 Triggering Shopify order creation...")
            success = await create_shopify_order(order['order_id'])
            
            if success:
                print("✅ Shopify order created successfully!")
                
                # Get updated order with Shopify ID
                updated_order_query = orders.select().where(
                    orders.c.order_id == order['order_id']
                )
                updated_order = await database.fetch_one(updated_order_query)
                
                if updated_order and updated_order['shopify_order_id']:
                    shopify_id = updated_order['shopify_order_id']
                    print(f"   Shopify Order ID: {shopify_id}")
                    print(f"   View in Shopify: https://chydantest.myshopify.com/admin/orders/{shopify_id}")
                    print(f"   Email will be sent to: {order['customer_email']}")
            else:
                print("❌ Failed to create Shopify order")
                print("   Check logs for details")
        else:
            print("No paid orders found without Shopify ID")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(update_merchant_shopify())

