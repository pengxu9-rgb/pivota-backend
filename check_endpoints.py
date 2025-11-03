#!/usr/bin/env python3
"""
检查端点是否正常工作
"""
import requests
import json

# 测试配置
BASE_URL = "https://web-production-fedb.up.railway.app"
AGENT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZ2VudEB0ZXN0LmNvbSIsInVzZXJfaWQiOiJhZ2VudEB0ZXN0LmNvbSIsImVtYWlsIjoiYWdlbnRAdGVzdC5jb20iLCJyb2xlIjoiYWdlbnQiLCJleHAiOjE3NjEzNzY2ODMsImlhdCI6MTc2MTI5MDI4M30.r-zzp405sULfwxNHcmxpl1GDQ3TdiQy77som-zE7Zqc"

# 关键端点测试
CRITICAL_ENDPOINTS = [
    # Health checks
    ("GET", "/", None, None, "Root health check"),
    ("GET", "/health", None, None, "Health endpoint"),
    
    # Agent API (Public)
    ("GET", "/agent/v1/health", None, None, "Agent API health"),
    ("GET", "/agent/v1/merchants", {"Authorization": f"Bearer {AGENT_TOKEN}"}, None, "Agent merchants"),
    ("GET", "/agent/metrics/summary", {"Authorization": f"Bearer {AGENT_TOKEN}"}, None, "Agent metrics"),
    
    # Auth
    ("GET", "/auth/me", {"Authorization": f"Bearer {AGENT_TOKEN}"}, None, "Auth check"),
    
    # Admin
    ("GET", "/admin/analytics/overview", {"Authorization": f"Bearer {AGENT_TOKEN}"}, None, "Admin analytics"),
    
    # Merchant
    ("GET", "/merchant/onboarding/all", {"Authorization": f"Bearer {AGENT_TOKEN}"}, None, "Merchant list"),
]

def test_endpoint(method, path, headers, data, description):
    """测试单个端点"""
    url = BASE_URL + path
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data, timeout=10)
        else:
            return f"❓ {description}: Unsupported method {method}"
        
        if response.status_code < 400:
            return f"✅ {description}: {response.status_code}"
        else:
            return f"❌ {description}: {response.status_code} - {response.text[:100]}"
            
    except requests.exceptions.Timeout:
        return f"⏱️ {description}: Timeout"
    except Exception as e:
        return f"💥 {description}: {str(e)[:100]}"

def main():
    print("=" * 60)
    print("ENDPOINT HEALTH CHECK")
    print("=" * 60)
    print(f"Testing against: {BASE_URL}")
    print()
    
    results = []
    for method, path, headers, data, description in CRITICAL_ENDPOINTS:
        result = test_endpoint(method, path, headers, data, description)
        results.append(result)
        print(result)
    
    # Summary
    print("\n" + "=" * 60)
    success_count = sum(1 for r in results if r.startswith("✅"))
    total_count = len(results)
    print(f"SUMMARY: {success_count}/{total_count} endpoints working")
    
    if success_count < total_count:
        print("\n⚠️ Some endpoints are failing!")
        print("This might be due to:")
        print("1. Authentication issues (token expired)")
        print("2. Deployment not complete")
        print("3. Actual endpoint problems")

if __name__ == "__main__":
    main()




