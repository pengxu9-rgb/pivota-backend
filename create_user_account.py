#!/usr/bin/env python3
"""Create user account for merchant via API"""

import requests

print("=== Creating User Account via API ===\n")

response = requests.post(
    "https://web-production-fedb.up.railway.app/auth/register",
    headers={"Content-Type": "application/json"},
    json={
        "email": "yao.wang@chydan.com",
        "password": "Merchant123!",
        "full_name": "Yao Wang",
        "role": "merchant"
    }
)

print(f"Status: {response.status_code}")
print(f"Response: {response.text}\n")

if response.status_code == 200:
    print("✅ Account created successfully!")
    print("\nLogin credentials:")
    print("  Email: yao.wang@chydan.com")
    print("  Password: Merchant123!")
    print("\n👉 You can now login to Merchant Portal")
elif response.status_code == 400 and "already registered" in response.text:
    print("✅ Account already exists!")
    print("\nLogin credentials:")
    print("  Email: yao.wang@chydan.com")
    print("  Password: Merchant123!")
    print("\n👉 You can now login to Merchant Portal")
else:
    print("❌ Failed to create account")
    print("You may need to contact support or use SQL directly")



