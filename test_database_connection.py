#!/usr/bin/env python3
"""
Test Database Connection on Railway
Check if PostgreSQL is properly connected
"""

import requests
import json
import time

BASE_URL = "https://web-production-fedb.up.railway.app"

print("🔍 Testing Database Connection")
print("="*60)

# Get admin token
token = requests.get(f"{BASE_URL}/auth/admin-token").json()["token"]
headers = {"Authorization": f"Bearer {token}"}

# 1. Check existing data (merchants persist = PostgreSQL working)
print("\n1️⃣ Checking persistent data:")
resp = requests.get(f"{BASE_URL}/merchant/onboarding/all", headers=headers)
if resp.status_code == 200:
    data = resp.json()
    merchant_count = data.get('count', 0)
    print(f"   Merchants found: {merchant_count}")
    if merchant_count > 0:
        print("   ✅ PostgreSQL is connected (merchants persist)")
    else:
        print("   ⚠️  No merchants found")

# 2. Test order creation and retrieval
print("\n2️⃣ Testing order persistence:")
order_data = {
    "merchant_id": "test_merchant_001",
    "customer_email": "test@example.com",
    "items": [{
        "product_id": f"db_test_{int(time.time())}",
        "product_title": "Database Test Product",
        "quantity": 1,
        "unit_price": "25.00",
        "subtotal": "25.00"
    }],
    "shipping_address": {
        "name": "DB Test",
        "address_line1": "123 Database St",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94105",
        "country": "US"
    },
    "currency": "USD"
}

# Create order
resp = requests.post(f"{BASE_URL}/orders/create", json=order_data, headers=headers)
if resp.status_code == 200:
    order = resp.json()
    order_id = order["order_id"]
    print(f"   Created order: {order_id}")
    
    # Wait for database commit
    time.sleep(3)
    
    # Try to retrieve
    check_resp = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
    
    if check_resp.status_code == 200:
        print(f"   ✅ Order persisted! Database is working!")
        retrieved = check_resp.json()
        print(f"   Confirmed: {retrieved['order_id']}")
    else:
        print(f"   ❌ Order NOT found - Database issue!")
        print(f"   Status: {check_resp.status_code}")
else:
    print(f"   ❌ Order creation failed: {resp.status_code}")

print("\n" + "="*60)
print("\n📊 DIAGNOSIS:")
print("")

if merchant_count > 0:
    print("✅ PostgreSQL IS connected (merchants table works)")
    print("❌ But orders table has issues")
    print("")
    print("Possible causes:")
    print("1. Orders table wasn't created properly")
    print("2. Transaction commit issue with orders")
    print("3. Different connection for orders vs merchants")
    print("")
    print("🔧 FIX: Try restarting the Railway service:")
    print("   1. Go to Railway dashboard")
    print("   2. Click on 'web' service")
    print("   3. Go to 'Settings' tab")
    print("   4. Click 'Restart' button")
else:
    print("⚠️  No data found - database might be empty")
    print("")
    print("Make sure in Railway:")
    print("1. Click on your 'web' service")
    print("2. Go to 'Variables' tab")
    print("3. Check if DATABASE_URL exists")
    print("4. It should reference your PostgreSQL service")
    print("   (something like: postgresql://postgres:xxx@postgres.railway.internal:5432/railway)")
