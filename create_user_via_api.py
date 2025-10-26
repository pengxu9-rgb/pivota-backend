#!/usr/bin/env python3
"""Create user account via API for existing merchant"""

import requests
import json

API_BASE = "https://web-production-fedb.up.railway.app"

print("=== Creating User Account for Merchant ===\n")

# Try to register user via public API
response = requests.post(
    f"{API_BASE}/api/auth/register",
    headers={"Content-Type": "application/json"},
    json={
        "email": "yao.wang@chydan.com",
        "password": "Merchant123!",
        "full_name": "Yao Wang",
        "role": "merchant"
    },
    timeout=10
)

print(f"Status: {response.status_code}")
try:
    data = response.json()
    print(f"Response: {json.dumps(data, indent=2)}\n")
except:
    print(f"Response: {response.text}\n")

if response.status_code in [200, 201]:
    print("✅ SUCCESS! User account created.\n")
    print("Login credentials:")
    print("  Email: yao.wang@chydan.com")
    print("  Password: Merchant123!")
    print("\n👉 You can now login to Merchant Portal")
elif response.status_code == 400:
    if "already registered" in response.text.lower():
        print("ℹ️  User already exists.\n")
        print("Login credentials:")
        print("  Email: yao.wang@chydan.com")
        print("  Password: Merchant123! (or your original password)")
        print("\n👉 Try logging in to Merchant Portal")
    else:
        print(f"❌ Error: {response.text}")
else:
    print(f"❌ Failed with status {response.status_code}")
    print("\nAlternative: Ask admin to reset password for this account")

