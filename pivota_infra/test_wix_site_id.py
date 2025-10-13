#!/usr/bin/env python3
"""
Test Wix Site ID
Test the provided Site ID with your Wix store
"""

import requests
import json
from typing import Optional

def test_wix_site_id():
    """Test the provided Site ID with Wix API"""
    
    # Your credentials
    api_key = "IST.eyJraWQiOiJQb3pIX2FDMiIsImFsZyI6IlJTMjU2In0.eyJkYXRhIjoie1wiaWRcIjpcImEwMWNmMzNiLTdmMGItNGNiMy1iNzZlLTA2NTcxODBkNTA0MlwiLFwiaWRlbnRpdHlcIjp7XCJ0eXBlXCI6XCJhcHBsaWNhdGlvblwiLFwiaWRcIjpcIjU5ODNjODNmLTlhMTMtNGY3Mi04YTU2LTBmMDcxM2QwMzY3OFwifSxcInRlbmFudFwiOntcInR5cGVcIjpcImFjY291bnRcIixcImlkXCI6XCJhYWQwNzY4Mi1iZTQ0LTQ0YjAtOGE0Mi01YjY2YjQ3MDAzMWRcIn19IiwiaWF0IjoxNzYwMzQ3NjIxfQ.RTf2gfv8f4yGd1vEH5WCHgL1sdkAXj-DFgx-a5RGEk9WOVKIGtQQm4P76u_yQ-yzkpDLHf8aW_6uQzgR4xOd-DOFkGCxJEJbYtt7-n-wqjR0n-9w5Us1qGcTM9w2MfcJZaac_8miqjG4eKjb9TCJ8Rv85BseqGEwQV6EMsdXBBTGAEr8FMgv_sUanh1RU_qCG0UBz-MY12FnLN91nEvWg4lt63A_lBxV3s2mNiodOgIrJadPrGY_gIxK3zBA-bToopYIFVbEHuhwkIZEQ79-acDdYbxgtycb_SmlBxrBBOFcHeuctpb-Lau3oS6Sz_-EfWOstquzPxEt0AgR40pgkQ"
    site_id = "c86b93b4-6d54-4e29-9bc3-26783c93ebb4"
    
    base_url = "https://www.wixapis.com"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'wix-site-id': site_id
    }
    
    print("🔍 **TESTING WIX SITE ID:**")
    print(f"   🆔 Site ID: {site_id}")
    print(f"   🌐 Store: https://peng652.wixsite.com/aydan-1")
    print("")
    
    # Test 1: Get products
    print("📦 **Test 1: Getting Products**")
    try:
        response = requests.get(f"{base_url}/stores/v1/products", headers=headers)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            products = data.get('products', [])
            print(f"   ✅ SUCCESS! Found {len(products)} products")
            
            if products:
                print("   📋 **Your Wix Products:**")
                for i, product in enumerate(products[:5]):  # Show first 5
                    print(f"      {i+1}. {product.get('name', 'Unknown')} - ${product.get('price', {}).get('value', 'N/A')}")
                if len(products) > 5:
                    print(f"      ... and {len(products) - 5} more products")
            return True
        else:
            print(f"   ❌ FAILED: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ ERROR: {e}")
        return False
    
    print("")
    
    # Test 2: Get orders (if products work)
    print("📋 **Test 2: Getting Orders**")
    try:
        response = requests.get(f"{base_url}/stores/v1/orders", headers=headers)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            orders = data.get('orders', [])
            print(f"   ✅ SUCCESS! Found {len(orders)} orders")
            return True
        else:
            print(f"   ❌ FAILED: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ ERROR: {e}")
        return False

def main():
    """Test the Wix Site ID"""
    success = test_wix_site_id()
    
    print("")
    if success:
        print("🎉 **SITE ID WORKS!**")
        print("   ✅ Your Wix store is now accessible")
        print("   ✅ Products can be fetched")
        print("   ✅ Orders can be created")
        print("")
        print("🔧 **Next Steps:**")
        print("   1. Update real_wix_adapter.py with this Site ID")
        print("   2. Test the full multi-merchant system")
        print("   3. Your Wix store will work with real products!")
    else:
        print("❌ **SITE ID DOESN'T WORK**")
        print("   ❌ API calls are failing")
        print("   ❌ Need to find correct Site ID")
        print("")
        print("🔧 **Alternative Options:**")
        print("   1. Try the manual JavaScript method")
        print("   2. Use alternative Wix integration")
        print("   3. Keep the hybrid mock system")

if __name__ == "__main__":
    main()
