#!/usr/bin/env python3
"""
自动批准流程测试脚本
Tests the intelligent auto-approval logic for merchant onboarding
"""

import requests
import json
import time
from datetime import datetime

# API Configuration
API_BASE_URL = "https://pivota-dashboard.onrender.com"

# Test Cases
test_cases = [
    {
        "name": "Test Case 1: High Match (Shopify) - Should Auto-Approve ✅",
        "data": {
            "business_name": "Acme Store",
            "store_url": "https://acme-shop.myshopify.com",
            "region": "US",
            "contact_email": "acme@test.com",
            "contact_phone": "+1-555-0100"
        },
        "expected_auto_approved": True,
        "expected_confidence_min": 0.7
    },
    {
        "name": "Test Case 2: Name Mismatch - Should Require Manual Review ❌",
        "data": {
            "business_name": "XYZ Corporation",
            "store_url": "https://totally-different-store.com",
            "region": "US",
            "contact_email": "info@xyz.com",
            "contact_phone": "+1-555-0400"
        },
        "expected_auto_approved": False,
        "expected_confidence_min": 0.0
    },
    {
        "name": "Test Case 3: Real Store (chydantest) - Should Auto-Approve ✅",
        "data": {
            "business_name": "Chydan Test Store",
            "store_url": "https://chydantest.myshopify.com",
            "region": "US",
            "contact_email": "chydan@test.com",
            "contact_phone": "+1-555-9999"
        },
        "expected_auto_approved": True,
        "expected_confidence_min": 0.7
    }
]

def print_header(text):
    """Print a formatted header"""
    print("\n" + "="*80)
    print(f"  {text}")
    print("="*80)

def print_section(text):
    """Print a formatted section"""
    print(f"\n{'─'*80}")
    print(f"  {text}")
    print(f"{'─'*80}")

def test_merchant_registration(test_case):
    """Test a single merchant registration case"""
    print_section(test_case["name"])
    
    print("\n📋 Input Data:")
    for key, value in test_case["data"].items():
        print(f"   {key}: {value}")
    
    print("\n🚀 Sending registration request...")
    
    try:
        # Send POST request
        response = requests.post(
            f"{API_BASE_URL}/merchant/onboarding/register",
            json=test_case["data"],
            timeout=30
        )
        
        # Check response status
        if response.status_code != 200:
            print(f"   ❌ ERROR: HTTP {response.status_code}")
            print(f"   Response: {response.text}")
            return False
        
        # Parse response
        result = response.json()
        
        print("\n✅ Registration Response:")
        print(f"   Status: {result.get('status')}")
        print(f"   Merchant ID: {result.get('merchant_id')}")
        print(f"   Auto-Approved: {result.get('auto_approved')}")
        print(f"   Confidence Score: {result.get('confidence_score', 0):.2%}")
        print(f"   Next Step: {result.get('next_step')}")
        
        if result.get('full_kyb_deadline'):
            print(f"   KYB Deadline: {result.get('full_kyb_deadline')}")
        
        # Show validation details
        if 'validation_details' in result:
            details = result['validation_details']
            print("\n🔍 Validation Details:")
            print(f"   URL Valid: {details.get('url_valid')}")
            print(f"   URL Message: {details.get('url_message')}")
            print(f"   Name Match: {details.get('name_match')}")
            print(f"   Match Score: {details.get('match_score', 0):.2%}")
            print(f"   Match Message: {details.get('match_message')}")
        
        # Show message
        print(f"\n💬 Message:")
        print(f"   {result.get('message')}")
        
        # Verify expectations
        print("\n🧪 Verification:")
        
        auto_approved = result.get('auto_approved', False)
        confidence = result.get('confidence_score', 0)
        
        # Check auto-approval expectation
        if auto_approved == test_case['expected_auto_approved']:
            print(f"   ✅ Auto-Approval: PASS (Expected: {test_case['expected_auto_approved']}, Got: {auto_approved})")
        else:
            print(f"   ❌ Auto-Approval: FAIL (Expected: {test_case['expected_auto_approved']}, Got: {auto_approved})")
        
        # Check confidence score
        if confidence >= test_case['expected_confidence_min']:
            print(f"   ✅ Confidence Score: PASS (Expected: ≥{test_case['expected_confidence_min']:.0%}, Got: {confidence:.2%})")
        else:
            print(f"   ⚠️  Confidence Score: Below Expected (Expected: ≥{test_case['expected_confidence_min']:.0%}, Got: {confidence:.2%})")
        
        # Check next step
        expected_next_step = "Connect PSP" if test_case['expected_auto_approved'] else "Wait for admin approval"
        if result.get('next_step') == expected_next_step:
            print(f"   ✅ Next Step: PASS (Expected: '{expected_next_step}')")
        else:
            print(f"   ⚠️  Next Step: Got '{result.get('next_step')}' (Expected: '{expected_next_step}')")
        
        # Overall result
        success = (
            auto_approved == test_case['expected_auto_approved'] and
            confidence >= test_case['expected_confidence_min']
        )
        
        if success:
            print("\n🎉 TEST PASSED!")
        else:
            print("\n⚠️  TEST COMPLETED WITH WARNINGS")
        
        return result
        
    except requests.exceptions.Timeout:
        print("   ❌ ERROR: Request timeout (30s)")
        return False
    except requests.exceptions.ConnectionError:
        print("   ❌ ERROR: Connection failed - is the backend running?")
        return False
    except Exception as e:
        print(f"   ❌ ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all test cases"""
    print_header("🧪 Auto-Approval Flow Test Suite")
    print(f"\nAPI Base URL: {API_BASE_URL}")
    print(f"Test Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total Test Cases: {len(test_cases)}")
    
    results = []
    
    for i, test_case in enumerate(test_cases, 1):
        result = test_merchant_registration(test_case)
        results.append({
            "test_case": test_case["name"],
            "success": result is not False,
            "result": result
        })
        
        # Wait between tests
        if i < len(test_cases):
            print("\n⏳ Waiting 2 seconds before next test...")
            time.sleep(2)
    
    # Summary
    print_header("📊 Test Summary")
    
    passed = sum(1 for r in results if r["success"])
    total = len(results)
    
    print(f"\nTotal Tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print(f"Success Rate: {passed/total*100:.1f}%")
    
    print("\n📋 Test Results:")
    for i, result in enumerate(results, 1):
        status = "✅ PASS" if result["success"] else "❌ FAIL"
        print(f"   {i}. {status} - {result['test_case']}")
    
    # Show merchant IDs for successful registrations
    merchant_ids = [r["result"].get("merchant_id") for r in results if r["success"] and r["result"]]
    if merchant_ids:
        print(f"\n🔑 Registered Merchant IDs:")
        for merchant_id in merchant_ids:
            print(f"   - {merchant_id}")
        print(f"\n💡 You can view these merchants in the admin dashboard:")
        print(f"   http://localhost:5173/")
        print(f"   Login: superadmin@pivota.com / admin123")
    
    print("\n" + "="*80)
    print("✅ Testing Complete!")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()

