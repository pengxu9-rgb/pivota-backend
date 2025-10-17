#!/usr/bin/env python3
"""
Agent SDK 测试脚本
演示如何创建 Agent 并使用 SDK
"""

import sys
import json
import requests
from datetime import datetime


BASE_URL = "https://web-production-fedb.up.railway.app"
# BASE_URL = "http://localhost:8000"  # 本地测试


def print_section(title: str):
    """打印分节标题"""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def print_success(message: str):
    """打印成功信息"""
    print(f"✅ {message}")


def print_error(message: str):
    """打印错误信息"""
    print(f"❌ {message}")


def print_info(message: str):
    """打印信息"""
    print(f"ℹ️  {message}")


def get_admin_token() -> str:
    """获取管理员 token"""
    response = requests.get(f"{BASE_URL}/auth/admin-token")
    if response.status_code == 200:
        return response.json()["token"]
    raise Exception("Failed to get admin token")


def create_test_agent(token: str) -> dict:
    """创建测试 Agent"""
    print_section("创建测试 Agent")
    
    agent_data = {
        "agent_name": f"Test Agent {datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "agent_type": "chatbot",
        "description": "SDK 测试 Agent",
        "owner_email": "test@example.com",
        "rate_limit": 100,
        "daily_quota": 10000,
        "allowed_merchants": None,  # 允许访问所有商户
        "metadata": {
            "created_by": "test_script",
            "purpose": "sdk_testing"
        }
    }
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(
        f"{BASE_URL}/agents/create",
        json=agent_data,
        headers=headers
    )
    
    if response.status_code == 200:
        result = response.json()
        agent = result["agent"]
        print_success(f"Agent 创建成功!")
        print_info(f"  - Agent ID: {agent['agent_id']}")
        print_info(f"  - Agent Name: {agent['agent_name']}")
        print_info(f"  - API Key: {agent['api_key']}")
        print_info(f"  ⚠️  请保存 API Key，它不会再次显示！")
        return agent
    else:
        print_error(f"创建失败: {response.status_code} - {response.text}")
        return None


def test_sdk_basic(api_key: str):
    """测试 SDK 基本功能"""
    print_section("测试 Agent SDK 基本功能")
    
    # 如果 SDK 已安装，使用它
    try:
        sys.path.insert(0, 'pivota_sdk')
        from pivota_agent import PivotaAgent
        
        print_info("使用 PivotaAgent SDK...")
        
        # 初始化 SDK
        agent = PivotaAgent(
            api_key=api_key,
            base_url=BASE_URL,
            debug=True
        )
        
        # 测试健康检查
        if agent.health_check():
            print_success("API 连接正常")
        else:
            print_error("API 连接失败")
        
        # 测试产品搜索
        print_info("\n搜索产品...")
        try:
            # 使用一个已连接的商户
            products = agent.search_products(
                merchant_id="merch_208139f7600dbf42",
                limit=5
            )
            print_success(f"找到 {products.get('total', 0)} 个产品")
            
            # 显示前几个产品
            for product in products.get("products", [])[:3]:
                print_info(f"  - {product.get('title', 'Unknown')} (${product.get('price', 0)})")
        except Exception as e:
            print_error(f"产品搜索失败: {e}")
        
        # 测试分析
        print_info("\n获取分析数据...")
        try:
            analytics = agent.get_analytics(days=7)
            print_success("分析数据获取成功")
            summary = analytics.get("analytics", {}).get("summary", {})
            print_info(f"  - 总请求数: {summary.get('total_requests', 0)}")
            print_info(f"  - 总订单数: {summary.get('total_orders', 0)}")
        except Exception as e:
            print_error(f"分析数据获取失败: {e}")
        
        agent.close()
        
    except ImportError:
        print_info("SDK 未安装，使用直接 API 调用...")
        test_direct_api(api_key)


def test_direct_api(api_key: str):
    """直接测试 API（不使用 SDK）"""
    
    headers = {"X-API-Key": api_key}
    
    # 测试产品搜索
    print_info("\n测试产品搜索 API...")
    response = requests.get(
        f"{BASE_URL}/agent/v1/products/search",
        params={
            "merchant_id": "merch_208139f7600dbf42",
            "limit": 5
        },
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        print_success(f"API 调用成功，找到 {data.get('total', 0)} 个产品")
    else:
        print_error(f"API 调用失败: {response.status_code} - {response.text}")
    
    # 测试速率限制头
    if "X-RateLimit-Limit" in response.headers:
        print_info(f"  - 速率限制: {response.headers['X-RateLimit-Limit']} 请求/分钟")
        print_info(f"  - 剩余配额: {response.headers.get('X-RateLimit-Remaining', 'N/A')}")


def test_order_creation(api_key: str):
    """测试订单创建流程"""
    print_section("测试订单创建流程")
    
    # 准备订单数据
    order_data = {
        "merchant_id": "merch_208139f7600dbf42",
        "customer_email": "agent-test@example.com",
        "items": [
            {
                "product_id": "test_product_1",
                "product_title": "Agent 测试产品",
                "quantity": 1,
                "unit_price": "25.00",
                "subtotal": "25.00"
            }
        ],
        "shipping_address": {
            "name": "Agent Test Customer",
            "address_line1": "456 Agent St",
            "city": "AI City",
            "postal_code": "00000",
            "country": "US"
        },
        "currency": "USD"
    }
    
    headers = {"X-API-Key": api_key}
    
    print_info("创建订单...")
    response = requests.post(
        f"{BASE_URL}/agent/v1/orders/create",
        json=order_data,
        headers=headers
    )
    
    if response.status_code == 200:
        order = response.json()
        print_success(f"订单创建成功!")
        print_info(f"  - 订单 ID: {order['order_id']}")
        print_info(f"  - 总金额: ${order['total']}")
        print_info(f"  - Agent Session: {order['tracking']['agent_session_id']}")
        
        # 查询订单状态
        print_info("\n查询订单状态...")
        status_response = requests.get(
            f"{BASE_URL}/agent/v1/orders/{order['order_id']}",
            headers=headers
        )
        
        if status_response.status_code == 200:
            order_status = status_response.json()["order"]
            print_success(f"订单状态: {order_status['status']}")
            print_info(f"  - 支付状态: {order_status['payment_status']}")
        
        return order
    else:
        print_error(f"订单创建失败: {response.status_code} - {response.text}")
        return None


def main():
    """主测试流程"""
    print("🤖" * 40)
    print("Agent SDK 测试")
    print("环境: " + BASE_URL)
    print("🤖" * 40)
    
    try:
        # 1. 获取管理员 token
        print_section("步骤 1: 获取管理员权限")
        admin_token = get_admin_token()
        print_success("管理员 token 获取成功")
        
        # 2. 创建测试 Agent
        agent = create_test_agent(admin_token)
        if not agent:
            print_error("无法创建 Agent，测试终止")
            return
        
        api_key = agent["api_key"]
        
        # 3. 测试 SDK 基本功能
        test_sdk_basic(api_key)
        
        # 4. 测试订单创建
        # test_order_creation(api_key)
        
        # 5. 查看 Agent 分析
        print_section("步骤 5: 查看 Agent 分析")
        headers = {"Authorization": f"Bearer {admin_token}"}
        response = requests.get(
            f"{BASE_URL}/agents/{agent['agent_id']}/analytics",
            params={"days": 1},
            headers=headers
        )
        
        if response.status_code == 200:
            analytics = response.json()["analytics"]
            print_success("分析数据获取成功")
            print_info(f"  - 总请求数: {analytics['summary'].get('total_requests', 0)}")
            print_info(f"  - 成功率: {analytics.get('success_rate', 0):.1f}%")
        
        print("\n" + "🤖" * 40)
        print("测试完成！")
        print(f"\n保存的 API Key 供后续使用:")
        print(f"  export PIVOTA_API_KEY='{api_key}'")
        print("🤖" * 40)
        
    except Exception as e:
        print_error(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
