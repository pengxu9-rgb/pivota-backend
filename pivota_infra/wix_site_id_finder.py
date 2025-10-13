#!/usr/bin/env python3
"""
Wix Site ID Finder
Helps you find your Wix site ID for API integration
"""

import requests
import json
from typing import Optional

class WixSiteIDFinder:
    def __init__(self, store_url: str, api_key: str):
        self.store_url = store_url
        self.api_key = api_key
        self.base_url = "https://www.wixapis.com"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
    
    def find_site_id_method_1(self) -> Optional[str]:
        """Method 1: Try to get site info from API"""
        try:
            # Try to get account info which might contain site information
            response = requests.get(f"{self.base_url}/sites/v1/sites", headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                sites = data.get('sites', [])
                if sites:
                    print(f"✅ Found {len(sites)} sites in your account:")
                    for site in sites:
                        print(f"   📍 Site: {site.get('displayName', 'Unknown')}")
                        print(f"   🆔 Site ID: {site.get('id', 'Unknown')}")
                        print(f"   🌐 URL: {site.get('url', 'Unknown')}")
                        print("")
                    return sites[0].get('id')
                else:
                    print("❌ No sites found in your account")
                    return None
            else:
                print(f"❌ Error getting sites: {response.status_code}")
                print(f"   Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
    
    def find_site_id_method_2(self) -> Optional[str]:
        """Method 2: Try different API endpoints"""
        try:
            # Try sites v2 API
            response = requests.get(f"{self.base_url}/sites/v2/sites", headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                sites = data.get('sites', [])
                if sites:
                    print(f"✅ Found {len(sites)} sites via v2 API:")
                    for site in sites:
                        print(f"   📍 Site: {site.get('displayName', 'Unknown')}")
                        print(f"   🆔 Site ID: {site.get('id', 'Unknown')}")
                        print(f"   🌐 URL: {site.get('url', 'Unknown')}")
                        print("")
                    return sites[0].get('id')
                else:
                    print("❌ No sites found via v2 API")
                    return None
            else:
                print(f"❌ Error getting sites via v2: {response.status_code}")
                return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
    
    def test_site_id(self, site_id: str) -> bool:
        """Test if a site ID works with the Stores API"""
        try:
            # Test with products endpoint
            response = requests.get(f"{self.base_url}/stores/v1/products", 
                                 headers={**self.headers, 'wix-site-id': site_id})
            
            if response.status_code == 200:
                data = response.json()
                products = data.get('products', [])
                print(f"✅ Site ID {site_id} works! Found {len(products)} products")
                return True
            elif response.status_code == 404:
                print(f"❌ Site ID {site_id} not found (404)")
                return False
            elif response.status_code == 403:
                print(f"❌ Site ID {site_id} forbidden (403) - check permissions")
                return False
            else:
                print(f"❌ Site ID {site_id} error: {response.status_code}")
                print(f"   Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Error testing site ID: {e}")
            return False
    
    def find_and_test_site_id(self) -> Optional[str]:
        """Find and test the correct site ID"""
        print("🔍 **FINDING YOUR WIX SITE ID:**")
        print("")
        
        # Method 1: Try sites v1 API
        print("📋 **Method 1: Sites v1 API**")
        site_id = self.find_site_id_method_1()
        if site_id:
            print(f"   🆔 Found site ID: {site_id}")
            if self.test_site_id(site_id):
                return site_id
            else:
                print("   ❌ Site ID doesn't work with Stores API")
        
        print("")
        
        # Method 2: Try sites v2 API
        print("📋 **Method 2: Sites v2 API**")
        site_id = self.find_site_id_method_2()
        if site_id:
            print(f"   🆔 Found site ID: {site_id}")
            if self.test_site_id(site_id):
                return site_id
            else:
                print("   ❌ Site ID doesn't work with Stores API")
        
        print("")
        print("❌ **Could not find working site ID**")
        print("")
        print("🔧 **Manual Steps to Find Site ID:**")
        print("   1. Go to your Wix dashboard")
        print("   2. Go to Settings > Custom Code")
        print("   3. Add this JavaScript code:")
        print("      console.log('Wix Site ID:', Wix.Utils.getInstanceId());")
        print("   4. Save and publish your site")
        print("   5. Visit your live site and check browser console")
        print("   6. Look for 'Wix Site ID' in the console output")
        print("")
        print("🌐 **Your Wix Store:** https://peng652.wixsite.com/aydan-1")
        
        return None

def main():
    """Find Wix site ID"""
    store_url = "peng652.wixsite.com/aydan-1"
    api_key = "IST.eyJraWQiOiJQb3pIX2FDMiIsImFsZyI6IlJTMjU2In0.eyJkYXRhIjoie1wiaWRcIjpcImEwMWNmMzNiLTdmMGItNGNiMy1iNzZlLTA2NTcxODBkNTA0MlwiLFwiaWRlbnRpdHlcIjp7XCJ0eXBlXCI6XCJhcHBsaWNhdGlvblwiLFwiaWRcIjpcIjU5ODNjODNmLTlhMTMtNGY3Mi04YTU2LTBmMDcxM2QwMzY3OFwifSxcInRlbmFudFwiOntcInR5cGVcIjpcImFjY291bnRcIixcImlkXCI6XCJhYWQwNzY4Mi1iZTQ0LTQ0YjAtOGE0Mi01YjY2YjQ3MDAzMWRcIn19IiwiaWF0IjoxNzYwMzQ3NjIxfQ.RTf2gfv8f4yGd1vEH5WCHgL1sdkAXj-DFgx-a5RGEk9WOVKIGtQQm4P76u_yQ-yzkpDLHf8aW_6uQzgR4xOd-DOFkGCxJEJbYtt7-n-wqjR0n-9w5Us1qGcTM9w2MfcJZaac_8miqjG4eKjb9TCJ8Rv85BseqGEwQV6EMsdXBBTGAEr8FMgv_sUanh1RU_qCG0UBz-MY12FnLN91nEvWg4lt63A_lBxV3s2mNiodOgIrJadPrGY_gIxK3zBA-bToopYIFVbEHuhwkIZEQ79-acDdYbxgtycb_SmlBxrBBOFcHeuctpb-Lau3oS6Sz_-EfWOstquzPxEt0AgR40pgkQ"
    
    finder = WixSiteIDFinder(store_url, api_key)
    site_id = finder.find_and_test_site_id()
    
    if site_id:
        print("")
        print("🎉 **SUCCESS!**")
        print(f"   🆔 Your Wix Site ID: {site_id}")
        print("")
        print("🔧 **Next Steps:**")
        print("   1. Update the real_wix_adapter.py with this site ID")
        print("   2. Test the Wix integration again")
        print("   3. Your multi-merchant system will be fully functional!")
    else:
        print("")
        print("❌ **Could not automatically find site ID**")
        print("")
        print("🔧 **Please follow the manual steps above**")
        print("   and provide the site ID when you find it.")

if __name__ == "__main__":
    main()
