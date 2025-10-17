#!/usr/bin/env python3
"""
完整订单流程测试
测试链路：库存检查 → 订单创建 → 支付确认 → Shopify 订单创建 → 履约跟踪
"""

import requests
import json
import time
from datetime import datetime
from typing import Dict, Any


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


def test_inventory_check(merchant_id: str, token: str):
    """测试库存检查（仅适用于已连接 Shopify 的商户）"""
    print_section("TEST: 库存检查")
    
    # 获取商户信息
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(f"{BASE_URL}/merchant/onboarding/{merchant_id}", headers=headers)
    
    if response.status_code != 200:
        print_error(f"无法获取商户信息: {response.status_code}")
        return
    
    merchant = response.json().get("merchant", {})
    
    if not merchant.get("mcp_connected"):
        print_info("商户未连接 MCP，跳过库存检查测试")
        return
    
    print_success(f"商户已连接 {merchant.get('mcp_platform')}")
    
    # 获取产品列表（用于找到 variant_id）
    if merchant.get("mcp_platform") == "shopify":
        print_info("获取 Shopify 产品列表...")
        # 这里可以调用产品 API 获取真实产品
        print_info("（库存检查将在订单创建时自动执行）")


def create_order_with_inventory(merchant_id: str, token: str) -> Dict[str, Any]:
    """创建订单（包含库存检查）"""
    print_section("TEST: 创建订单（含库存检查）")
    
    # 准备订单数据
    # 注意：如果要测试真实库存，需要提供真实的 variant_id
    order_data = {
        "merchant_id": merchant_id,
        "customer_email": "test@example.com",
        "items": [
            {
                "product_id": "test_product_1",
                "product_title": "测试产品 A",
                "variant_id": None,  # 如果有真实 variant_id，在这里填写
                "sku": "TEST-SKU-001",
                "quantity": 1,
                "unit_price": "99.99",
                "subtotal": "99.99"
            }
        ],
        "shipping_address": {
            "name": "Test Customer",
            "address_line1": "123 Test Street",
            "city": "Test City",
            "postal_code": "12345",
            "country": "US"
        },
        "currency": "USD",
        "agent_session_id": f"test_session_{int(time.time())}",
        "metadata": {
            "test_run": True,
            "timestamp": datetime.now().isoformat()
        }
    }
    
    print_info(f"创建订单，商品数量: {len(order_data['items'])}")
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(
        f"{BASE_URL}/orders/create",
        json=order_data,
        headers=headers
    )
    
    if response.status_code == 200:
        order = response.json()
        print_success(f"订单创建成功: {order['order_id']}")
        print_info(f"  - 状态: {order['status']}")
        print_info(f"  - 总金额: ${order['total']}")
        print_info(f"  - Payment Intent: {order.get('payment_intent_id', 'N/A')}")
        print_info(f"  - Client Secret: {order.get('client_secret', 'N/A')[:20]}..." if order.get('client_secret') else "")
        return order
    elif response.status_code == 400:
        error = response.json()
        if "Insufficient inventory" in str(error):
            print_error("库存不足！")
            print_info(f"详情: {json.dumps(error, indent=2, ensure_ascii=False)}")
        else:
            print_error(f"订单创建失败: {error}")
    else:
        print_error(f"订单创建失败: {response.status_code} - {response.text}")
    
    return None


def confirm_payment(order: Dict[str, Any], token: str) -> bool:
    """确认支付（模拟）"""
    print_section("TEST: 支付确认")
    
    if not order.get("client_secret"):
        print_error("订单没有 payment intent，可能商户未配置有效的 PSP 密钥")
        return False
    
    payment_data = {
        "order_id": order["order_id"],
        "payment_method_id": "pm_card_visa"  # Stripe 测试卡
    }
    
    print_info(f"确认支付: {order['order_id']}")
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(
        f"{BASE_URL}/orders/payment/confirm",
        json=payment_data,
        headers=headers
    )
    
    if response.status_code == 200:
        result = response.json()
        print_success(f"支付状态: {result['payment_status']}")
        print_info(f"  - 消息: {result.get('message', '')}")
        
        # 检查是否触发了 Shopify 订单创建
        if result["payment_status"] == "succeeded":
            print_info("✨ 后台任务已触发：创建 Shopify 订单")
        
        return result["payment_status"] == "succeeded"
    else:
        print_error(f"支付确认失败: {response.status_code} - {response.text}")
        return False


def check_order_status(order_id: str, token: str):
    """检查订单状态"""
    print_section("TEST: 订单状态检查")
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(f"{BASE_URL}/orders/{order_id}", headers=headers)
    
    if response.status_code == 200:
        order = response.json()
        print_success(f"订单 {order_id}")
        print_info(f"  - 状态: {order['status']}")
        print_info(f"  - 支付状态: {order['payment_status']}")
        print_info(f"  - 履约状态: {order.get('fulfillment_status', 'N/A')}")
        print_info(f"  - Shopify 订单: {order.get('shopify_order_id', 'N/A')}")
        print_info(f"  - 跟踪号: {order.get('tracking_number', 'N/A')}")
        return order
    else:
        print_error(f"获取订单失败: {response.status_code}")
        return None


def test_webhook_registration(merchant_id: str, token: str):
    """测试 Webhook 注册"""
    print_section("TEST: Webhook 注册")
    
    webhook_data = {
        "callback_base_url": BASE_URL
    }
    
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(
        f"{BASE_URL}/webhooks/register/shopify/{merchant_id}",
        json=webhook_data,
        headers=headers
    )
    
    if response.status_code == 200:
        result = response.json()
        print_success(f"Webhook 注册成功")
        for webhook in result.get("registered_webhooks", []):
            print_info(f"  - {webhook['topic']}: ID {webhook['webhook_id']}")
    else:
        print_error(f"Webhook 注册失败: {response.status_code} - {response.text}")


def main():
    """主测试流程"""
    print("🎯" * 40)
    print("完整订单流程测试")
    print("测试环境：" + BASE_URL)
    print("🎯" * 40)
    
    try:
        # 1. 获取 token
        print_section("STEP 1: 获取管理员 Token")
        token = get_admin_token()
        print_success(f"Token 获取成功: {token[:20]}...")
        
        # 2. 选择测试商户
        # 使用已连接 PSP 和 Shopify 的商户
        merchant_id = "merch_208139f7600dbf42"  # chydantest 商户
        print_section("STEP 2: 选择测试商户")
        print_info(f"使用商户: {merchant_id}")
        
        # 3. 测试库存检查
        test_inventory_check(merchant_id, token)
        
        # 4. 创建订单（含库存检查）
        order = create_order_with_inventory(merchant_id, token)
        
        if order:
            # 5. 确认支付
            time.sleep(2)  # 等待 payment intent 创建
            payment_success = confirm_payment(order, token)
            
            if payment_success:
                # 6. 等待后台任务完成
                print_section("STEP 6: 等待后台任务")
                print_info("等待 Shopify 订单创建（5 秒）...")
                time.sleep(5)
                
                # 7. 检查最终状态
                final_order = check_order_status(order["order_id"], token)
                
                if final_order and final_order.get("shopify_order_id"):
                    print_success("🎉 完整订单流程测试成功！")
                    print_info(f"  - Pivota 订单: {final_order['order_id']}")
                    print_info(f"  - Shopify 订单: {final_order['shopify_order_id']}")
                else:
                    print_info("订单已创建和支付，但 Shopify 订单可能还在处理中")
            
            # 8. 测试 Webhook 注册（可选）
            # test_webhook_registration(merchant_id, token)
        
        print("\n" + "🎯" * 40)
        print("测试完成！")
        print("🎯" * 40)
        
    except Exception as e:
        print_error(f"测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
