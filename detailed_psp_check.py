#!/usr/bin/env python3
"""
Detailed PSP configuration check
"""
import requests
import json

API_BASE_URL = "https://web-production-fedb.up.railway.app"
AGENT_API_KEY = "ak_live_ee029e36064d52dcdac1db24181efe38e8466ed94bff6a5f04252bde8db1f684"
MERCHANT_ID = "merch_208139f7600dbf42"

print("=" * 70)
print("🔍 DETAILED PSP CONFIGURATION CHECK")
print("=" * 70)
print(f"Merchant ID: {MERCHANT_ID}\n")

# Test each PSP individually with minimal order
psps_to_test = {
    "stripe": "Stripe (should work - in env vars)",
    "adyen": "Adyen (configured via Employee Portal)",
    "checkout": "Checkout.com (configured via Employee Portal)",
    "paypal": "PayPal (configured via Employee Portal)"
}

results = {}

for psp, description in psps_to_test.items():
    print(f"\n{'─'*70}")
    print(f"Testing: {psp.upper()}")
    print(f"Description: {description}")
    print(f"{'─'*70}")
    
    # Create minimal order
    order_data = {
        "merchant_id": MERCHANT_ID,
        "preferred_psp": psp,
        "items": [{
            "product_id": f"{psp}_check",
            "product_title": "Config Check",
            "quantity": 1,
            "unit_price": 1.00,
            "subtotal": 1.00
        }],
        "customer_email": "check@test.com",
        "shipping_address": {
            "name": "Check",
            "address_line1": "123 St",
            "city": "NYC",
            "state": "NY",
            "postal_code": "10001",
            "country": "US"
        },
        "subtotal": 1.00,
        "shipping_fee": 0,
        "tax": 0.08,
        "total": 1.08,
        "currency": "USD"
    }
    
    headers = {
        "x-api-key": AGENT_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(
            f"{API_BASE_URL}/agent/v1/orders/create",
            headers=headers,
            json=order_data,
            timeout=15
        )
        
        if response.status_code == 200:
            data = response.json()
            payment = data.get('payment', {})
            
            has_payment_intent = payment.get('payment_intent_id') is not None
            has_client_secret = payment.get('client_secret') is not None
            
            results[psp] = {
                "configured": has_payment_intent,
                "order_id": data.get('order_id'),
                "payment_intent": payment.get('payment_intent_id'),
                "has_secret": has_client_secret
            }
            
            if has_payment_intent:
                print(f"✅ CONFIGURED")
                print(f"   Payment Intent: {payment.get('payment_intent_id')}")
                if has_client_secret:
                    secret = payment.get('client_secret')
                    if secret and secret.startswith('http'):
                        print(f"   Type: Redirect URL")
                        print(f"   URL: {secret[:50]}...")
                    else:
                        print(f"   Type: Client Secret")
                        print(f"   Secret: {secret[:30] if secret else 'None'}...")
            else:
                print(f"❌ NOT CONFIGURED")
                print(f"   Payment Intent: null")
                print(f"   Client Secret: null")
                print(f"   → PSP API key not found in database")
        else:
            print(f"❌ API Error: {response.status_code}")
            results[psp] = {"configured": False, "error": response.status_code}
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        results[psp] = {"configured": False, "error": str(e)}

# Final Summary
print(f"\n\n{'='*70}")
print("📊 CONFIGURATION STATUS")
print("=" * 70)

for psp, result in results.items():
    status = "✅ WORKING" if result.get('configured') else "❌ NOT CONFIGURED"
    print(f"{psp.upper():15} {status}")

print(f"\n{'─'*70}")
configured_count = sum(1 for r in results.values() if r.get('configured'))
print(f"Total Configured: {configured_count}/4")
print(f"Success Rate: {configured_count/4*100:.0f}%")
print("=" * 70)

print(f"\n💡 DIAGNOSIS:")
if configured_count == 1:
    print("   Only Stripe is working (from environment variables)")
    print("   Adyen, Checkout, PayPal configs are NOT saved in database")
    print("")
    print("   Possible issues:")
    print("   1. Database transaction still not committing")
    print("   2. Configuration saved to wrong merchant_id")
    print("   3. Employee Portal using old cached code")
    print("   4. Railway backend not fully reloaded")
elif configured_count == 4:
    print("   🎉 All PSPs are configured and working!")
else:
    print(f"   Partial success: {configured_count}/4 PSPs working")

print("\n📝 Next step: Check Railway logs for PSP save attempts")


