"""
Test API login endpoint
"""

import asyncio
import json
from fastapi.testclient import TestClient
from main import app

def test_login_endpoint():
    """Test the login endpoint directly"""
    client = TestClient(app)
    
    print("🔍 Testing login endpoint...")
    
    # Test admin login
    response = client.post(
        "/api/dashboard/auth/login",
        json={"username": "admin", "password": "admin123"}
    )
    
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Login successful!")
        print(f"   Token: {data.get('access_token', 'N/A')[:20]}...")
        print(f"   User: {data.get('user', {})}")
    else:
        print(f"❌ Login failed: {response.text}")

if __name__ == "__main__":
    test_login_endpoint()
