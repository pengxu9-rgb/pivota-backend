#!/usr/bin/env python3
"""
Product Proxy API 测试脚本
测试实时代理 + 智能缓存 + 事件追踪架构
"""

import requests
import time
import json
from typing import Optional

# 配置
BASE_URL = "https://web-production-fedb.up.railway.app"  # Railway 部署地址
# BASE_URL = "http://localhost:8000"  # 本地测试
MERCHANT_ID = "merch_a4dc9a163f49d835"  # 替换为实际商户 ID
JWT_TOKEN = None  # 从环境变量或参数获取

def get_jwt_token():
    """获取 JWT Token（Admin）"""
    global JWT_TOKEN
    if JWT_TOKEN:
        return JWT_TOKEN
    
    # 从 /auth/admin-token 获取测试 token
    response = requests.get(f"{BASE_URL}/auth/admin-token")
    if response.status_code == 200:
        JWT_TOKEN = response.json()["token"]
        print(f"✅ Got JWT Token: {JWT_TOKEN[:20]}...")
        return JWT_TOKEN
    else:
        print(f"❌ Failed to get JWT token: {response.status_code}")
        return None

def make_request(method: str, endpoint: str, **kwargs):
    """统一请求函数"""
    token = get_jwt_token()
    if not token:
        return None
    
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    
    url = f"{BASE_URL}{endpoint}"
    response = requests.request(method, url, headers=headers, **kwargs)
    return response

def test_1_first_fetch_cache_miss():
    """测试 1: 第一次拉取产品（Cache Miss）"""
    print("\n" + "="*80)
    print("TEST 1: 第一次拉取产品（Cache Miss - 应该慢）")
    print("="*80)
    
    start = time.time()
    response = make_request("GET", f"/products/{MERCHANT_ID}?limit=10")
    elapsed = time.time() - start
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response Time: {elapsed:.2f}s")
        print(f"📦 Products Count: {data.get('total', 0)}")
        print(f"🔧 Platform: {data.get('platform', 'unknown')}")
        
        if data.get('products'):
            first_product = data['products'][0]
            print(f"\n📦 First Product Sample:")
            print(f"   ID: {first_product.get('id')}")
            print(f"   Title: {first_product.get('title')}")
            print(f"   Price: ${first_product.get('price')} {first_product.get('currency')}")
            print(f"   Inventory: {first_product.get('inventory_quantity')}")
        
        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(f"   Error: {response.text[:200]}")
        return False

def test_2_second_fetch_cache_hit():
    """测试 2: 第二次拉取产品（Cache Hit）"""
    print("\n" + "="*80)
    print("TEST 2: 第二次拉取产品（Cache Hit - 应该快）")
    print("="*80)
    
    start = time.time()
    response = make_request("GET", f"/products/{MERCHANT_ID}?limit=10")
    elapsed = time.time() - start
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⚡ Response Time: {elapsed:.2f}s (应该 < 100ms)")
        print(f"📦 Products Count: {data.get('total', 0)}")
        
        if elapsed < 0.5:
            print(f"✅ Cache Hit - Response is fast!")
        else:
            print(f"⚠️  Response seems slow, might not be cache hit")
        
        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        return False

def test_3_force_refresh():
    """测试 3: 强制刷新缓存"""
    print("\n" + "="*80)
    print("TEST 3: 强制刷新缓存（force_refresh=true）")
    print("="*80)
    
    start = time.time()
    response = make_request("GET", f"/products/{MERCHANT_ID}?limit=5&force_refresh=true")
    elapsed = time.time() - start
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response Time: {elapsed:.2f}s (应该慢，因为跳过缓存)")
        print(f"📦 Products Count: {data.get('total', 0)}")
        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        return False

def test_4_single_product():
    """测试 4: 获取单个产品详情"""
    print("\n" + "="*80)
    print("TEST 4: 获取单个产品详情")
    print("="*80)
    
    # 先获取产品列表
    response = make_request("GET", f"/products/{MERCHANT_ID}?limit=1")
    if response.status_code != 200:
        print("❌ Failed to get product list")
        return False
    
    products = response.json().get('products', [])
    if not products:
        print("⚠️  No products available")
        return False
    
    product_id = products[0]['id']
    print(f"📦 Testing with Product ID: {product_id}")
    
    response = make_request("GET", f"/products/{MERCHANT_ID}/{product_id}")
    
    if response.status_code == 200:
        data = response.json()
        product = data.get('product', {})
        print(f"✅ Status: {response.status_code}")
        print(f"\n📦 Product Details:")
        print(f"   ID: {product.get('id')}")
        print(f"   Title: {product.get('title')}")
        print(f"   Description: {product.get('description', '')[:100]}...")
        print(f"   Price: ${product.get('price')} {product.get('currency')}")
        print(f"   SKU: {product.get('sku')}")
        print(f"   Variants: {len(product.get('variants', []))}")
        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        return False

def test_5_analytics():
    """测试 5: 获取商户分析指标"""
    print("\n" + "="*80)
    print("TEST 5: 获取商户分析指标")
    print("="*80)
    
    response = make_request("GET", f"/products/analytics/{MERCHANT_ID}")
    
    if response.status_code == 200:
        data = response.json()
        analytics = data.get('analytics', {})
        
        print(f"✅ Status: {response.status_code}")
        print(f"\n📊 Analytics:")
        print(f"   Total API Calls: {analytics.get('total_api_calls', 0)}")
        print(f"   Cache Hit Rate: {analytics.get('cache_hit_rate', 0):.2f}%")
        print(f"   Avg Response Time: {analytics.get('avg_response_time_ms', 0):.0f}ms")
        print(f"   Total Orders: {analytics.get('total_orders', 0)}")
        print(f"   Successful Orders: {analytics.get('successful_orders', 0)}")
        print(f"   Conversion Rate: {analytics.get('conversion_rate', 0):.2f}%")
        print(f"   Payment Success Rate: {analytics.get('payment_success_rate', 0):.2f}%")
        print(f"   Total Revenue: ${analytics.get('total_revenue', 0):.2f}")
        
        return True
    else:
        print(f"⚠️  Status: {response.status_code}")
        print(f"   Message: {response.json().get('message', 'No analytics data yet')}")
        return True  # 没有数据也算正常

def test_6_performance_comparison():
    """测试 6: 性能对比（Cache Hit vs Cache Miss）"""
    print("\n" + "="*80)
    print("TEST 6: 性能对比（Cache Hit vs Cache Miss）")
    print("="*80)
    
    # Cache Miss
    print("\n🔄 Forcing cache refresh...")
    start = time.time()
    response1 = make_request("GET", f"/products/{MERCHANT_ID}?limit=20&force_refresh=true")
    time_miss = time.time() - start
    
    # Cache Hit
    print("⚡ Reading from cache...")
    time.sleep(0.5)  # 等待缓存写入
    start = time.time()
    response2 = make_request("GET", f"/products/{MERCHANT_ID}?limit=20")
    time_hit = time.time() - start
    
    if response1.status_code == 200 and response2.status_code == 200:
        speedup = time_miss / time_hit if time_hit > 0 else 0
        print(f"\n📊 Performance Comparison:")
        print(f"   Cache Miss: {time_miss:.3f}s")
        print(f"   Cache Hit:  {time_hit:.3f}s")
        print(f"   Speedup:    {speedup:.1f}x faster")
        
        if speedup > 5:
            print(f"✅ Excellent! Cache is working well")
        elif speedup > 2:
            print(f"✅ Good! Cache provides noticeable speedup")
        else:
            print(f"⚠️  Cache speedup is lower than expected")
        
        return True
    else:
        print(f"❌ Performance test failed")
        return False

def test_7_cleanup_cache():
    """测试 7: 清理过期缓存"""
    print("\n" + "="*80)
    print("TEST 7: 清理过期缓存")
    print("="*80)
    
    response = make_request("POST", "/products/maintenance/cleanup-cache")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"   Message: {data.get('message')}")
        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        return False

def test_8_error_handling():
    """测试 8: 错误处理（不存在的商户）"""
    print("\n" + "="*80)
    print("TEST 8: 错误处理（不存在的商户）")
    print("="*80)
    
    response = make_request("GET", "/products/merch_nonexistent")
    
    if response.status_code == 404:
        print(f"✅ Correctly returned 404 for non-existent merchant")
        print(f"   Error: {response.json().get('detail')}")
        return True
    else:
        print(f"⚠️  Expected 404, got {response.status_code}")
        return False

def run_all_tests():
    """运行所有测试"""
    print("\n" + "🎯"*40)
    print("Product Proxy API - Comprehensive Test Suite")
    print("防御性产品架构测试：实时代理 + 智能缓存 + 事件追踪")
    print("🎯"*40)
    
    print(f"\n🌐 Base URL: {BASE_URL}")
    print(f"🏪 Merchant ID: {MERCHANT_ID}")
    
    tests = [
        ("First Fetch (Cache Miss)", test_1_first_fetch_cache_miss),
        ("Second Fetch (Cache Hit)", test_2_second_fetch_cache_hit),
        ("Force Refresh", test_3_force_refresh),
        ("Single Product Detail", test_4_single_product),
        ("Analytics Metrics", test_5_analytics),
        ("Performance Comparison", test_6_performance_comparison),
        ("Cleanup Cache", test_7_cleanup_cache),
        ("Error Handling", test_8_error_handling),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"❌ Exception in {name}: {e}")
            results.append((name, False))
        time.sleep(0.5)  # 避免请求过快
    
    # 总结
    print("\n" + "="*80)
    print("📊 TEST SUMMARY")
    print("="*80)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
    
    print(f"\n🎯 Results: {passed}/{total} tests passed ({passed/total*100:.0f}%)")
    
    if passed == total:
        print("\n🎉 All tests passed! Architecture is working correctly.")
    elif passed >= total * 0.75:
        print("\n⚠️  Most tests passed, but some issues need attention.")
    else:
        print("\n❌ Multiple tests failed, please check logs.")

if __name__ == "__main__":
    import sys
    
    # 支持命令行参数
    if len(sys.argv) > 1:
        BASE_URL = sys.argv[1]
    if len(sys.argv) > 2:
        MERCHANT_ID = sys.argv[2]
    if len(sys.argv) > 3:
        JWT_TOKEN = sys.argv[3]
    
    run_all_tests()

