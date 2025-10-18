"""
Manually trigger Shopify order creation for a paid order
"""
import requests
import json

BASE_URL = 'https://web-production-fedb.up.railway.app'
ORDER_ID = 'ORD_8B95A09EC04E1965'  # The paid order

# Get admin token
token_resp = requests.get(f'{BASE_URL}/auth/admin-token')
token = token_resp.json()['token']
headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json'
}

print("🛍️ Manually triggering Shopify order creation...")
print("="*60)

# First, check the order
resp = requests.get(f'{BASE_URL}/orders/{ORDER_ID}', headers=headers)
if resp.status_code == 200:
    order = resp.json()
    print(f"Order: {ORDER_ID}")
    print(f"Payment Status: {order.get('payment_status')}")
    print(f"Customer: {order.get('customer_email')}")
    
    if order.get('payment_status') == 'paid':
        print("\n✅ Order is paid, can create Shopify order")
        
        # Try to trigger fulfillment endpoint if it exists
        fulfill_resp = requests.post(
            f'{BASE_URL}/orders/{ORDER_ID}/fulfill',
            headers=headers
        )
        
        if fulfill_resp.status_code == 200:
            print("✅ Fulfillment triggered!")
        else:
            print(f"Fulfillment endpoint: {fulfill_resp.status_code}")
            
        # Alternative: Try shipping endpoint
        ship_data = {
            "tracking_number": "TEST-TRACKING-001",
            "carrier": "Shopify"
        }
        
        ship_resp = requests.post(
            f'{BASE_URL}/orders/{ORDER_ID}/ship',
            json=ship_data,
            headers=headers
        )
        
        if ship_resp.status_code == 200:
            print("✅ Shipping endpoint triggered!")
        else:
            print(f"Shipping endpoint: {ship_resp.status_code}")
    else:
        print(f"⚠️ Order not paid: {order.get('payment_status')}")

