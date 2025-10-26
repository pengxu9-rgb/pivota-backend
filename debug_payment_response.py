#!/usr/bin/env python3
"""
Debug payment response to see what's actually returned
"""
import requests
import json
from datetime import datetime

# Configuration
API_BASE_URL = "https://web-production-fedb.up.railway.app"
AGENT_API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
MERCHANT_ID = "merch_208139f7600dbf42"

def test_psp_order(psp: str):
    """Test order creation for a specific PSP"""
    
    order_data = {
        "merchant_id": MERCHANT_ID,
        "preferred_psp": psp,
        "items": [{
            "product_id": f"debug_{psp}",
            "product_title": f"Debug Product for {psp.upper()}",
            "quantity": 1,
            "unit_price": 19.99,
            "subtotal": 19.99
        }],
        "customer_email": f"debug.{psp}@test.com",
        "shipping_address": {
            "name": f"Debug {psp.upper()}",
            "address_line1": "123 Debug St",
            "city": "New York",
            "state": "NY",
            "postal_code": "10001",
            "country": "US"
        },
        "subtotal": 19.99,
        "shipping_fee": 0,
        "tax": 1.60,
        "total": 21.59,
        "currency": "USD"
    }
    
    headers = {
        "x-api-key": AGENT_API_KEY,
        "Content-Type": "application/json"
    }
    
    print(f"\n{'='*60}")
    print(f"Testing {psp.upper()} Payment Response")
    print(f"{'='*60}")
    
    try:
        response = requests.post(
            f"{API_BASE_URL}/agent/v1/orders/create",
            headers=headers,
            json=order_data
        )
        
        print(f"\nStatus Code: {response.status_code}")
        print(f"\nRaw Response:")
        print(response.text)
        
        if response.status_code == 200:
            data = response.json()
            print(f"\nParsed Response:")
            print(json.dumps(data, indent=2))
            
            # Check payment data
            if "payment" in data:
                payment = data["payment"]
                print(f"\nPayment Details:")
                print(f"  client_secret: {payment.get('client_secret', 'N/A')}")
                print(f"  payment_intent_id: {payment.get('payment_intent_id', 'N/A')}")
                print(f"  instructions: {payment.get('instructions', 'N/A')}")
                
                if payment.get('client_secret'):
                    print(f"  ✅ Payment intent created successfully")
                else:
                    print(f"  ❌ No payment intent returned")
            else:
                print(f"\n❌ No payment data in response")
        else:
            print(f"\n❌ Failed with status {response.status_code}")
            
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")

def main():
    """Test all PSPs"""
    print("🔍 Debugging Payment Responses")
    print(f"API: {API_BASE_URL}")
    print(f"Merchant: {MERCHANT_ID}")
    
    # Test each PSP
    for psp in ["stripe", "adyen", "checkout"]:
        test_psp_order(psp)
        print()

if __name__ == "__main__":
    main()
