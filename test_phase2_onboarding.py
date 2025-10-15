#!/usr/bin/env python3
"""
Test script for Phase 2 Merchant Onboarding
Tests the complete merchant onboarding flow
"""

import requests
import time
import json

# API Base URL
API_URL = "https://pivota-dashboard.onrender.com"
# API_URL = "http://localhost:8000"  # For local testing

def test_merchant_onboarding():
    print("🚀 Testing Phase 2 Merchant Onboarding Flow\n")
    
    # Step 1: Register merchant
    print("📝 Step 1: Registering merchant...")
    register_data = {
        "business_name": "Test Coffee Shop",
        "website": "https://testcoffee.com",
        "region": "US",
        "contact_email": f"coffee_{int(time.time())}@test.com",  # Unique email
        "contact_phone": "+1-555-0123"
    }
    
    response = requests.post(f"{API_URL}/merchant/onboarding/register", json=register_data)
    print(f"   Status: {response.status_code}")
    print(f"   Response: {json.dumps(response.json(), indent=2)}\n")
    
    if response.status_code != 200:
        print("❌ Registration failed!")
        return
    
    merchant_id = response.json()["merchant_id"]
    print(f"✅ Merchant registered: {merchant_id}\n")
    
    # Step 2: Wait for auto-KYC approval
    print("⏳ Step 2: Waiting for auto-KYC approval (5 seconds)...")
    time.sleep(6)  # Wait 6 seconds to be safe
    
    # Check status
    response = requests.get(f"{API_URL}/merchant/onboarding/status/{merchant_id}")
    print(f"   Status: {response.status_code}")
    status_data = response.json()
    print(f"   KYC Status: {status_data['kyc_status']}")
    print(f"   PSP Connected: {status_data['psp_connected']}\n")
    
    if status_data['kyc_status'] != 'approved':
        print("❌ KYC not approved yet. Retry or check admin dashboard.")
        return
    
    print("✅ KYC approved!\n")
    
    # Step 3: Connect PSP
    print("🔌 Step 3: Connecting Stripe PSP...")
    psp_data = {
        "merchant_id": merchant_id,
        "psp_type": "stripe",
        "psp_sandbox_key": "sk_test_fake_key_for_testing"
    }
    
    response = requests.post(f"{API_URL}/merchant/onboarding/psp/setup", json=psp_data)
    print(f"   Status: {response.status_code}")
    psp_response = response.json()
    print(f"   Response: {json.dumps(psp_response, indent=2)}\n")
    
    if response.status_code != 200:
        print("❌ PSP setup failed!")
        return
    
    api_key = psp_response["api_key"]
    print(f"✅ PSP connected successfully!\n")
    print(f"🔑 YOUR API KEY (save this!):\n   {api_key}\n")
    
    # Step 4: Verify final status
    print("🔍 Step 4: Verifying final status...")
    response = requests.get(f"{API_URL}/merchant/onboarding/status/{merchant_id}")
    final_status = response.json()
    print(f"   Merchant ID: {final_status['merchant_id']}")
    print(f"   Business: {final_status['business_name']}")
    print(f"   KYC Status: {final_status['kyc_status']}")
    print(f"   PSP Connected: {final_status['psp_connected']}")
    print(f"   PSP Type: {final_status.get('psp_type', 'N/A')}")
    print(f"   API Key Issued: {final_status['api_key_issued']}\n")
    
    print("=" * 60)
    print("🎉 ONBOARDING COMPLETE!")
    print("=" * 60)
    print(f"\nNext steps:")
    print(f"1. Use this API key in payment requests:")
    print(f"   Header: X-Merchant-API-Key: {api_key}")
    print(f"\n2. Test payment execution:")
    print(f"   curl -X POST {API_URL}/payment/execute \\")
    print(f"     -H 'X-Merchant-API-Key: {api_key}' \\")
    print(f"     -H 'Content-Type: application/json' \\")
    print(f"     -d '{{\"order_id\":\"TEST-001\",\"amount\":50.00,\"currency\":\"USD\"}}'")
    print(f"\n3. View in admin dashboard:")
    print(f"   {API_URL.replace('onrender.com', 'lovable.app') if 'onrender' in API_URL else API_URL}/admin")
    print()

if __name__ == "__main__":
    try:
        test_merchant_onboarding()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


