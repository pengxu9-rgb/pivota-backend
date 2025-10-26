#!/usr/bin/env python3
"""
Quick test for Adyen and PayPal configurations
"""

import asyncio
import httpx
import base64
import sys

async def test_adyen(api_key):
    """Test Adyen API key"""
    print("\n🔍 Testing Adyen...")
    
    if not api_key.startswith("AQE"):
        print("❌ Invalid Adyen API key format. Must start with 'AQE'")
        return False
    
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    payload = {
        "merchantAccount": "PivotaTestMerchant"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://checkout-test.adyen.com/v70/paymentMethods",
                json=payload,
                headers=headers,
                timeout=10.0
            )
            
            print(f"Response: {response.status_code}")
            if response.status_code == 200:
                print("✅ Adyen API key is valid!")
                return True
            else:
                print(f"❌ Failed: {response.text[:200]}")
                return False
                
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

async def test_paypal(client_id, client_secret):
    """Test PayPal credentials"""
    print("\n🔍 Testing PayPal...")
    
    try:
        auth_string = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-m.sandbox.paypal.com/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data="grant_type=client_credentials",
                timeout=10.0
            )
            
            print(f"Response: {response.status_code}")
            if response.status_code == 200:
                print("✅ PayPal credentials are valid!")
                token = response.json()["access_token"]
                print(f"Token: {token[:20]}...")
                return True
            else:
                print(f"❌ Failed: {response.text[:200]}")
                return False
                
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

async def main():
    print("="*60)
    print("PSP QUICK TEST")
    print("="*60)
    
    if len(sys.argv) < 2:
        print("\nUsage:")
        print("  Test Adyen:  python3 quick_test_psps.py adyen <API_KEY>")
        print("  Test PayPal: python3 quick_test_psps.py paypal <CLIENT_ID> <CLIENT_SECRET>")
        print("\nExample:")
        print("  python3 quick_test_psps.py adyen AQEhhmfuXNWTK0Qc+...")
        print("  python3 quick_test_psps.py paypal AWnRr... EPH3q...")
        return
    
    provider = sys.argv[1].lower()
    
    if provider == "adyen":
        if len(sys.argv) < 3:
            print("❌ Please provide Adyen API key")
            return
        await test_adyen(sys.argv[2])
        
    elif provider == "paypal":
        if len(sys.argv) < 4:
            print("❌ Please provide PayPal Client ID and Secret")
            return
        await test_paypal(sys.argv[2], sys.argv[3])
        
    else:
        print(f"❌ Unknown provider: {provider}")
        print("   Use 'adyen' or 'paypal'")

if __name__ == "__main__":
    asyncio.run(main())
