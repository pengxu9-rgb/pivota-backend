#!/bin/bash

echo "🔍 Checking product count for each merchant"
echo "============================================"
echo ""

# Use python to query the API
python3 << 'PYTHON_SCRIPT'
import urllib.request
import json

API_URL = "https://web-production-fedb.up.railway.app"

# Get products cache
try:
    url = f"{API_URL}/test/check-products-cache"
    data = json.loads(urllib.request.urlopen(url).read().decode())
    
    # Count products by merchant
    products_by_merchant = {}
    
    if "sample" in data:
        # This is just a sample, let's count from the full data if available
        print(f"Total products in cache: {data.get('total_products', 0)}")
        print("")
        
        for product in data.get("sample", []):
            merchant_id = product.get("merchant_id")
            if merchant_id:
                if merchant_id not in products_by_merchant:
                    products_by_merchant[merchant_id] = []
                products_by_merchant[merchant_id].append({
                    "id": product.get("platform_product_id"),
                    "cached_at": product.get("cached_at"),
                    "expires_at": product.get("expires_at")
                })
    
    print("📊 Product count by merchant (from sample):")
    print("-" * 60)
    for merchant_id, products in products_by_merchant.items():
        print(f"{merchant_id}: {len(products)} products")
        for p in products[:2]:  # Show first 2
            print(f"  - {p['id']} (cached: {p['cached_at'][:19]})")
        if len(products) > 2:
            print(f"  ... and {len(products) - 2} more")
        print("")
    
except Exception as e:
    print(f"❌ Error: {e}")

PYTHON_SCRIPT

echo ""
echo "============================================"
echo "💡 To verify a specific merchant:"
echo "Check Employee Portal → Merchants page → Products column"





