#!/usr/bin/env python3
"""
Wix Store Diagnostic
Check your Wix store status and subscription issues
"""

import requests
import json
from typing import Dict, Any, Optional

def check_wix_store_status():
    """Check your Wix store status and identify issues"""
    
    # Your credentials
    api_key = "IST.eyJraWQiOiJQb3pIX2FDMiIsImFsZyI6IlJTMjU2In0.eyJkYXRhIjoie1wiaWRcIjpcImEwMWNmMzNiLTdmMGItNGNiMy1iNzZlLTA2NTcxODBkNTA0MlwiLFwiaWRlbnRpdHlcIjp7XCJ0eXBlXCI6XCJhcHBsaWNhdGlvblwiLFwiaWRcIjpcIjU5ODNjODNmLTlhMTMtNGY3Mi04YTU2LTBmMDcxM2QwMzY3OFwifSxcInRlbmFudFwiOntcInR5cGVcIjpcImFjY291bnRcIixcImlkXCI6XCJhYWQwNzY4Mi1iZTQ0LTQ0YjAtOGE0Mi01YjY2YjQ3MDAzMWRcIn19IiwiaWF0IjoxNzYwMzQ3NjIxfQ.RTf2gfv8f4yGd1vEH5WCHgL1sdkAXj-DFgx-a5RGEk9WOVKIGtQQm4P76u_yQ-yzkpDLHf8aW_6uQzgR4xOd-DOFkGCxJEJbYtt7-n-wqjR0n-9w5Us1qGcTM9w2MfcJZaac_8miqjG4eKjb9TCJ8Rv85BseqGEwQV6EMsdXBBTGAEr8FMgv_sUanh1RU_qCG0UBz-MY12FnLN91nEvWg4lt63A_lBxV3s2mNiodOgIrJadPrGY_gIxK3zBA-bToopYIFVbEHuhwkIZEQ79-acDdYbxgtycb_SmlBxrBBOFcHeuctpb-Lau3oS6Sz_-EfWOstquzPxEt0AgR40pgkQ"
    store_url = "peng652.wixsite.com/aydan-1"
    
    base_url = "https://www.wixapis.com"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    print("🔍 **WIX STORE DIAGNOSTIC:**")
    print(f"   🌐 Store: https://{store_url}")
    print(f"   🔑 API Key: {api_key[:20]}...")
    print("")
    
    # Test 1: Check account info
    print("📋 **Test 1: Account Information**")
    try:
        response = requests.get(f"{base_url}/sites/v1/sites", headers=headers)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            sites = data.get('sites', [])
            print(f"   ✅ Account accessible - {len(sites)} sites found")
            
            for site in sites:
                print(f"      📍 Site: {site.get('displayName', 'Unknown')}")
                print(f"      🆔 ID: {site.get('id', 'Unknown')}")
                print(f"      🌐 URL: {site.get('url', 'Unknown')}")
                print(f"      📊 Status: {site.get('status', 'Unknown')}")
        else:
            print(f"   ❌ Account access failed: {response.status_code}")
            print(f"   Response: {response.text}")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print("")
    
    # Test 2: Check API permissions
    print("📋 **Test 2: API Permissions**")
    try:
        # Try different API endpoints to check permissions
        endpoints = [
            "/stores/v1/products",
            "/stores/v1/orders", 
            "/sites/v1/sites",
            "/apps/v1/apps"
        ]
        
        for endpoint in endpoints:
            response = requests.get(f"{base_url}{endpoint}", headers=headers)
            print(f"   {endpoint}: {response.status_code}")
            
            if response.status_code == 403:
                print(f"      ❌ Forbidden - Check subscription/permissions")
            elif response.status_code == 404:
                print(f"      ❌ Not found - Endpoint may not exist")
            elif response.status_code == 200:
                print(f"      ✅ Accessible")
            else:
                print(f"      ⚠️  Unexpected status")
                
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print("")
    
    # Test 3: Check subscription status
    print("📋 **Test 3: Subscription Status**")
    try:
        # Try to get account details
        response = requests.get(f"{base_url}/sites/v1/sites", headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            sites = data.get('sites', [])
            
            if sites:
                site = sites[0]
                print(f"   📊 Site Status: {site.get('status', 'Unknown')}")
                print(f"   🏪 Plan: {site.get('plan', 'Unknown')}")
                print(f"   🔒 Permissions: {site.get('permissions', 'Unknown')}")
                
                # Check if e-commerce is enabled
                if 'stores' in str(site.get('features', [])):
                    print(f"   🛒 E-commerce: Enabled")
                else:
                    print(f"   🛒 E-commerce: Not enabled")
            else:
                print(f"   ❌ No sites found in account")
        else:
            print(f"   ❌ Cannot access account info: {response.status_code}")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print("")
    
    # Test 4: Check specific store access
    print("📋 **Test 4: Store Access**")
    try:
        # Try to access your specific store
        response = requests.get(f"{base_url}/stores/v1/products", headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            products = data.get('products', [])
            print(f"   ✅ Store accessible - {len(products)} products found")
        elif response.status_code == 403:
            print(f"   ❌ Store access forbidden - Subscription issue")
            print(f"   💡 Solution: Upgrade your Wix plan to enable e-commerce")
        elif response.status_code == 404:
            print(f"   ❌ Store not found - Check store URL")
        else:
            print(f"   ❌ Store access failed: {response.status_code}")
            print(f"   Response: {response.text}")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print("")
    
    # Provide recommendations
    print("💡 **RECOMMENDATIONS:**")
    print("")
    print("🔧 **If you're getting 403 Forbidden errors:**")
    print("   1. Check your Wix subscription plan")
    print("   2. Ensure e-commerce is enabled")
    print("   3. Verify API permissions")
    print("   4. Consider upgrading to a paid plan")
    print("")
    print("🔧 **If you're getting 404 errors:**")
    print("   1. Verify your store URL is correct")
    print("   2. Check if the store is published")
    print("   3. Ensure the store is accessible")
    print("")
    print("🔧 **Alternative Solutions:**")
    print("   1. Use the alternative Wix integration (already working)")
    print("   2. Create orders via Wix dashboard manually")
    print("   3. Use Wix forms for order collection")
    print("   4. Set up webhook integration")

def main():
    """Run Wix store diagnostic"""
    check_wix_store_status()
    
    print("")
    print("🌐 **Your Wix Store:** https://peng652.wixsite.com/aydan-1")
    print("")
    print("📞 **Next Steps:**")
    print("   1. Check your Wix dashboard for subscription status")
    print("   2. Ensure e-commerce is enabled")
    print("   3. Verify API permissions")
    print("   4. Consider upgrading your plan if needed")

if __name__ == "__main__":
    main()
