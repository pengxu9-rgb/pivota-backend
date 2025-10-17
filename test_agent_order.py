#!/usr/bin/env python3
"""
Test Agent Order Creation Flow
Creates an agent, then uses it to create an order
"""

import sys
import json
import requests
import time
from datetime import datetime

# Add SDK to path
sys.path.insert(0, 'pivota_sdk')

BASE_URL = "https://web-production-fedb.up.railway.app"
MERCHANT_ID = "merch_208139f7600dbf42"
CUSTOMER_EMAIL = "peng@chydan.com"


def print_step(msg):
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print('=' * 60)


def get_admin_token():
    resp = requests.get(f"{BASE_URL}/auth/admin-token")
    return resp.json()["token"]


def create_agent(token):
    """Create a test agent"""
    print_step("Creating Agent")
    
    agent_data = {
        "agent_name": f"Test Agent {datetime.now().strftime('%H%M%S')}",
        "agent_type": "test",
        "description": "E2E Test Agent",
        "rate_limit": 100,
        "daily_quota": 1000,
        "owner_email": CUSTOMER_EMAIL
    }
    
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(f"{BASE_URL}/agents/create", json=agent_data, headers=headers)
    
    if resp.status_code == 200:
        data = resp.json()
        agent_data = data.get('agent', data)  # Handle nested response
        print(f"✅ Agent created: {agent_data['agent_id']}")
        print(f"   API Key: {agent_data['api_key']}")
        return agent_data
    else:
        print(f"❌ Failed: {resp.text}")
        return None


def test_agent_order(api_key):
    """Test order creation with agent"""
    print_step("Creating Order via Agent API")
    
    # Import SDK
    try:
        from pivota_agent import PivotaAgent
    except ImportError:
        print("❌ SDK not found, using direct API calls")
        return test_direct_api(api_key)
    
    # Use SDK to create order
    agent = PivotaAgent(api_key=api_key, base_url=BASE_URL)
    
    try:
        # Create order using SDK method signature
        items = [
            {
                "product_id": "agent_test_001",
                "product_title": "Agent Test Product",
                "quantity": 1,
                "unit_price": "99.99",
                "subtotal": "99.99"
            }
        ]
        
        shipping_address = {
            "name": "Agent Test Customer",
            "address_line1": "789 Tech Ave",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US"
        }
        
        order = agent.create_order(
            merchant_id=MERCHANT_ID,
            customer_email=CUSTOMER_EMAIL,
            items=items,
            shipping_address=shipping_address,
            currency="USD"
        )
        print(f"✅ Order created via Agent: {order['order_id']}")
        print(f"   Total: ${order['total']} {order['currency']}")
        print(f"   Status: {order['status']}")
        return order
        
    except Exception as e:
        print(f"❌ SDK Error: {e}")
        return None


def test_direct_api(api_key):
    """Test order creation directly via API"""
    print("   Using direct API call...")
    
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    order_data = {
        "merchant_id": MERCHANT_ID,
        "customer_email": CUSTOMER_EMAIL,
        "items": [
            {
                "product_id": "agent_test_002",
                "product_title": "Direct API Test Product",
                "quantity": 2,
                "unit_price": "49.99",
                "subtotal": "99.98"
            }
        ],
        "shipping_address": {
            "name": "Direct API Test",
            "address_line1": "456 API Street",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94104",
            "country": "US"
        },
        "currency": "USD"
    }
    
    resp = requests.post(
        f"{BASE_URL}/agent/v1/orders/create",
        json=order_data,
        headers=headers
    )
    
    if resp.status_code == 200:
        order = resp.json()
        print(f"✅ Order created via Direct API: {order['order_id']}")
        print(f"   Total: ${order['total']} {order['currency']}")
        print(f"   Status: {order['status']}")
        return order
    else:
        print(f"❌ Direct API Failed: {resp.status_code} - {resp.text[:200]}")
        return None


def check_order_status(token, order_id):
    """Check order status"""
    print_step("Checking Order Status")
    
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
    
    if resp.status_code == 200:
        order = resp.json()
        print(f"✅ Order Status:")
        print(f"   ID: {order['order_id']}")
        print(f"   Payment Status: {order['payment_status']}")
        print(f"   Shopify Order: {order.get('shopify_order_id', 'Not created yet')}")
        return order
    else:
        print(f"❌ Failed to get order status")
        return None


def main():
    print("\n" + "🤖" * 30)
    print("AGENT ORDER TEST")
    print(f"Environment: {BASE_URL}")
    print(f"Merchant: {MERCHANT_ID}")
    print("🤖" * 30)
    
    try:
        # 1. Get admin token
        print("\n1. Getting admin token...")
        token = get_admin_token()
        print("✅ Token obtained")
        
        # 2. Create agent
        agent = create_agent(token)
        if not agent:
            return
        
        # 3. Test order creation with agent
        order = test_agent_order(agent['api_key'])
        
        if order:
            # 4. Check order status
            time.sleep(2)
            check_order_status(token, order['order_id'])
            
            print_step("TEST RESULTS")
            print(f"✅ Agent ID: {agent['agent_id']}")
            print(f"✅ API Key: {agent['api_key'][:20]}...")
            print(f"✅ Order ID: {order['order_id']}")
            print(f"✅ Order Total: ${order['total']}")
            
            print("\n💡 Note: Payment intent and Shopify order creation")
            print("   depend on valid PSP credentials in the system")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
