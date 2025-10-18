"""
Script to update merchant with Shopify credentials
"""
import requests
import json

BASE_URL = 'https://web-production-fedb.up.railway.app'
MERCHANT_ID = 'merch_208139f7600dbf42'

# Shopify credentials from Railway environment
SHOPIFY_STORE = 'chydantest.myshopify.com'
SHOPIFY_TOKEN = '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'

def main():
    print("🔧 Updating merchant with Shopify credentials...")
    print("="*70)
    
    # Get admin token
    token_resp = requests.get(f'{BASE_URL}/auth/admin-token')
    token = token_resp.json()['token']
    headers = {'Authorization': f'Bearer {token}'}
    
    # Try to use the merchant update endpoint
    # Since we need to update mcp_connected, mcp_shop_domain, and mcp_access_token
    # but these fields aren't exposed in regular endpoints, we need a workaround
    
    # Option 1: Try the onboarding complete endpoint
    onboarding_data = {
        "merchant_id": MERCHANT_ID,
        "mcp_connected": True,
        "mcp_shop_domain": SHOPIFY_STORE,
        "mcp_access_token": SHOPIFY_TOKEN,
        "mcp_type": "shopify"
    }
    
    print(f"Merchant ID: {MERCHANT_ID}")
    print(f"Shopify Store: {SHOPIFY_STORE}")
    print(f"Token: {SHOPIFY_TOKEN[:20]}...")
    
    # Try to complete MCP setup
    resp = requests.post(
        f"{BASE_URL}/merchant/onboarding/complete",
        json=onboarding_data,
        headers=headers
    )
    
    if resp.status_code == 200:
        print("✅ Merchant updated with Shopify credentials!")
    else:
        print(f"Status code: {resp.status_code}")
        # Try alternative approach - direct database update via admin endpoint
        # This would require a custom endpoint to be added
        
    # Now trigger Shopify order creation for a paid order
    print("\n📦 Triggering Shopify order creation...")
    
    # Get a paid order without Shopify ID
    orders_resp = requests.get(f'{BASE_URL}/orders', headers=headers)
    if orders_resp.status_code == 200:
        orders = orders_resp.json()
        paid_orders = [o for o in orders if o.get('payment_status') == 'paid' and not o.get('shopify_order_id')]
        
        if paid_orders:
            order = paid_orders[0]
            order_id = order['order_id']
            print(f"Found paid order: {order_id}")
            print(f"Customer: {order['customer_email']}")
            print(f"Total: ${order['total']}")
            
            # The create_shopify_order function should be triggered automatically
            # after payment confirmation, but we can check if it worked
            print("\nShopify order should be created automatically after payment.")
            print("Check: https://chydantest.myshopify.com/admin/orders")

if __name__ == "__main__":
    main()

