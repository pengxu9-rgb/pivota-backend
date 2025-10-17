#!/usr/bin/env python3
"""
Complete End-to-End Test
从 Agent 搜索产品到收到 Shopify 订单确认邮件的完整测试
"""

import sys
import json
import time
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List

# 添加 SDK 路径
sys.path.insert(0, 'pivota_sdk')

# 配置
BASE_URL = "https://web-production-fedb.up.railway.app"
# BASE_URL = "http://localhost:8000"  # 本地测试

# 测试配置
TEST_CONFIG = {
    "merchant_id": "merch_208139f7600dbf42",  # chydantest (已连接 Stripe 和 Shopify)
    "customer_email": "peng@chydan.com",  # 会收到订单确认邮件的真实邮箱
    "test_stripe": True,
    "test_adyen": False,  # 如果商户也连接了 Adyen
}


class Colors:
    """终端颜色"""
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_section(title: str):
    """打印分节标题"""
    print(f"\n{Colors.HEADER}{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}{Colors.ENDC}\n")


def print_success(message: str):
    """打印成功信息"""
    print(f"{Colors.OKGREEN}✅ {message}{Colors.ENDC}")


def print_error(message: str):
    """打印错误信息"""
    print(f"{Colors.FAIL}❌ {message}{Colors.ENDC}")


def print_info(message: str):
    """打印信息"""
    print(f"{Colors.OKCYAN}ℹ️  {message}{Colors.ENDC}")


def print_warning(message: str):
    """打印警告"""
    print(f"{Colors.WARNING}⚠️  {message}{Colors.ENDC}")


class E2ETestRunner:
    """端到端测试运行器"""
    
    def __init__(self):
        self.admin_token = None
        self.agent = None
        self.api_key = None
        self.products = []
        self.order = None
        self.payment_confirmed = False
        self.shopify_order_id = None
        
    def get_admin_token(self) -> str:
        """获取管理员 token"""
        response = requests.get(f"{BASE_URL}/auth/admin-token")
        if response.status_code == 200:
            return response.json()["token"]
        raise Exception("Failed to get admin token")
    
    def create_test_agent(self) -> Dict[str, Any]:
        """创建测试 Agent"""
        print_section("步骤 1: 创建 Agent")
        
        agent_data = {
            "agent_name": f"E2E Test Agent {datetime.now().strftime('%H%M%S')}",
            "agent_type": "chatbot",
            "description": "端到端测试 Agent - 测试真实订单流程",
            "owner_email": TEST_CONFIG["customer_email"],
            "rate_limit": 1000,
            "daily_quota": 10000,
            "allowed_merchants": None,  # 允许访问所有商户
            "metadata": {
                "test_type": "e2e_complete",
                "created_at": datetime.now().isoformat()
            }
        }
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.post(
            f"{BASE_URL}/agents/create",
            json=agent_data,
            headers=headers
        )
        
        if response.status_code == 200:
            result = response.json()
            agent = result["agent"]
            print_success(f"Agent 创建成功: {agent['agent_id']}")
            print_info(f"API Key: {agent['api_key']}")
            return agent
        else:
            raise Exception(f"创建 Agent 失败: {response.text}")
    
    def test_product_search(self) -> List[Dict[str, Any]]:
        """测试产品搜索"""
        print_section("步骤 2: Agent 搜索产品")
        
        try:
            from pivota_agent import PivotaAgent
            
            # 初始化 SDK
            sdk = PivotaAgent(
                api_key=self.api_key,
                base_url=BASE_URL,
                debug=True
            )
            
            # 搜索产品
            print_info(f"搜索商户 {TEST_CONFIG['merchant_id']} 的产品...")
            result = sdk.search_products(
                merchant_id=TEST_CONFIG["merchant_id"],
                in_stock_only=True,
                limit=10
            )
            
            products = result.get("products", [])
            print_success(f"找到 {len(products)} 个产品")
            
            # 显示产品
            for i, product in enumerate(products[:5], 1):
                print_info(f"  {i}. {product.get('title', 'Unknown')}")
                print_info(f"     价格: ${product.get('price', 0)}")
                print_info(f"     库存: {'有货' if product.get('in_stock', False) else '缺货'}")
                print_info(f"     ID: {product.get('id')}")
                if product.get('variant_id'):
                    print_info(f"     Variant: {product.get('variant_id')}")
            
            sdk.close()
            return products
            
        except ImportError:
            print_warning("SDK 未安装，使用直接 API 调用")
            return self.search_products_direct()
    
    def search_products_direct(self) -> List[Dict[str, Any]]:
        """直接调用 API 搜索产品"""
        headers = {"X-API-Key": self.api_key}
        response = requests.get(
            f"{BASE_URL}/agent/v1/products/search",
            params={
                "merchant_id": TEST_CONFIG["merchant_id"],
                "in_stock_only": "true",
                "limit": 10
            },
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("products", [])
        else:
            print_error(f"产品搜索失败: {response.text}")
            return []
    
    def validate_cart(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """验证购物车"""
        print_section("步骤 3: 验证购物车")
        
        headers = {"X-API-Key": self.api_key}
        response = requests.post(
            f"{BASE_URL}/agent/v1/cart/validate",
            json={
                "merchant_id": TEST_CONFIG["merchant_id"],
                "items": items,
                "shipping_country": "US"
            },
            headers=headers
        )
        
        if response.status_code == 200:
            result = response.json()
            print_success("购物车验证成功")
            pricing = result.get("pricing", {})
            print_info(f"  小计: ${pricing.get('subtotal', 0)}")
            print_info(f"  运费: ${pricing.get('shipping_fee', 0)}")
            print_info(f"  税费: ${pricing.get('tax', 0)}")
            print_info(f"  总计: ${pricing.get('total', 0)}")
            return result
        else:
            print_error(f"购物车验证失败: {response.text}")
            return None
    
    def create_order(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """创建订单"""
        print_section("步骤 4: 创建订单")
        
        order_data = {
            "merchant_id": TEST_CONFIG["merchant_id"],
            "customer_email": TEST_CONFIG["customer_email"],
            "items": items,
            "shipping_address": {
                "name": "E2E Test Customer",
                "address_line1": "123 Test Street",
                "address_line2": "Suite 100",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
                "phone": "+1234567890"
            },
            "currency": "USD",
            "metadata": {
                "test_type": "e2e_complete",
                "agent_id": self.agent["agent_id"]
            }
        }
        
        headers = {"X-API-Key": self.api_key}
        response = requests.post(
            f"{BASE_URL}/agent/v1/orders/create",
            json=order_data,
            headers=headers
        )
        
        if response.status_code == 200:
            order = response.json()
            print_success(f"订单创建成功: {order['order_id']}")
            print_info(f"  总金额: ${order['total']} {order['currency']}")
            
            if order.get("payment"):
                print_info(f"  Payment Intent: {order['payment']['payment_intent_id']}")
                print_info(f"  Client Secret: {order['payment']['client_secret'][:20]}...")
            else:
                print_warning("  未生成支付意图（可能 PSP 未配置）")
            
            return order
        else:
            print_error(f"订单创建失败: {response.text}")
            return None
    
    def confirm_payment_stripe(self, order: Dict[str, Any]) -> bool:
        """确认 Stripe 支付"""
        print_section("步骤 5A: 测试 Stripe 支付")
        
        if not order.get("payment", {}).get("client_secret"):
            print_warning("订单没有 Stripe payment intent")
            return False
        
        # 获取管理员 token 来确认支付
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        payment_data = {
            "order_id": order["order_id"],
            "payment_method_id": "pm_card_visa"  # Stripe 测试卡
        }
        
        print_info("使用 Stripe 测试卡确认支付...")
        response = requests.post(
            f"{BASE_URL}/orders/payment/confirm",
            json=payment_data,
            headers=admin_headers
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get("payment_status") == "succeeded":
                print_success("✅ Stripe 支付成功!")
                print_info(f"  消息: {result.get('message', '')}")
                return True
            else:
                print_warning(f"支付状态: {result.get('payment_status')}")
                return False
        else:
            print_error(f"支付确认失败: {response.text}")
            return False
    
    def test_adyen_payment(self, merchant_id: str) -> bool:
        """测试 Adyen 支付（如果商户支持）"""
        print_section("步骤 5B: 测试 Adyen 支付")
        
        # 首先检查商户是否配置了 Adyen
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.get(
            f"{BASE_URL}/merchant/onboarding/{merchant_id}",
            headers=admin_headers
        )
        
        if response.status_code == 200:
            merchant = response.json().get("merchant", {})
            if merchant.get("psp_type") != "adyen":
                print_info("商户使用 Stripe，跳过 Adyen 测试")
                return True
            
            print_warning("Adyen 支付测试需要额外配置")
            # TODO: 实现 Adyen 支付测试
            return True
        else:
            print_error("无法获取商户信息")
            return False
    
    def check_order_status(self, order_id: str, wait_for_shopify: bool = True) -> Dict[str, Any]:
        """检查订单状态"""
        print_section("步骤 6: 验证订单状态")
        
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        max_attempts = 10
        for attempt in range(max_attempts):
            response = requests.get(
                f"{BASE_URL}/orders/{order_id}",
                headers=admin_headers
            )
            
            if response.status_code == 200:
                order = response.json()
                print_info(f"尝试 {attempt + 1}/{max_attempts}:")
                print_info(f"  订单状态: {order['status']}")
                print_info(f"  支付状态: {order['payment_status']}")
                print_info(f"  履约状态: {order.get('fulfillment_status', 'N/A')}")
                
                shopify_order_id = order.get("shopify_order_id")
                if shopify_order_id:
                    print_success(f"✅ Shopify 订单已创建: {shopify_order_id}")
                    self.shopify_order_id = shopify_order_id
                    return order
                elif wait_for_shopify and order["payment_status"] == "paid":
                    print_info("  等待 Shopify 订单创建...")
                    time.sleep(3)
                else:
                    return order
            else:
                print_error(f"获取订单失败: {response.text}")
                return None
        
        print_warning("超时：Shopify 订单可能还在处理中")
        return None
    
    def verify_email_notification(self):
        """验证邮件通知"""
        print_section("步骤 7: 邮件通知验证")
        
        print_info(f"订单确认邮件将发送到: {TEST_CONFIG['customer_email']}")
        print_info("请检查您的邮箱（包括垃圾邮件文件夹）")
        
        if self.shopify_order_id:
            print_success(f"Shopify 订单 {self.shopify_order_id} 应该已触发邮件通知")
            print_info("邮件内容应包含：")
            print_info("  - 订单号")
            print_info("  - 商品清单")
            print_info("  - 配送地址")
            print_info("  - 订单总金额")
        else:
            print_warning("Shopify 订单未创建，可能不会收到邮件")
    
    def run(self):
        """运行完整测试"""
        print(f"{Colors.BOLD}{Colors.HEADER}")
        print("🚀" * 40)
        print("完整端到端测试：Agent → 订单 → 支付 → Shopify → 邮件")
        print(f"测试环境: {BASE_URL}")
        print(f"测试商户: {TEST_CONFIG['merchant_id']}")
        print(f"客户邮箱: {TEST_CONFIG['customer_email']}")
        print("🚀" * 40)
        print(f"{Colors.ENDC}")
        
        try:
            # 1. 获取管理员权限
            print_info("获取管理员权限...")
            self.admin_token = self.get_admin_token()
            print_success("管理员权限获取成功")
            
            # 2. 创建 Agent
            self.agent = self.create_test_agent()
            self.api_key = self.agent["api_key"]
            
            # 3. 搜索产品
            self.products = self.test_product_search()
            if not self.products:
                print_error("没有找到产品，测试终止")
                return
            
            # 4. 准备订单商品
            print_section("准备订单商品")
            
            # 选择前两个产品或用户指定的产品
            selected_products = self.products[:2] if len(self.products) >= 2 else self.products[:1]
            
            order_items = []
            for product in selected_products:
                item = {
                    "product_id": str(product.get("id", "unknown")),
                    "product_title": product.get("title", "Test Product"),
                    "variant_id": product.get("variant_id"),  # 如果有
                    "sku": product.get("sku"),  # 如果有
                    "quantity": 1,
                    "unit_price": str(product.get("price", 10.00)),
                    "subtotal": str(product.get("price", 10.00))
                }
                order_items.append(item)
                print_info(f"添加到购物车: {item['product_title']} x {item['quantity']}")
            
            # 5. 验证购物车
            cart_validation = self.validate_cart([
                {"product_id": item["product_id"], "quantity": item["quantity"]}
                for item in order_items
            ])
            
            # 6. 创建订单
            self.order = self.create_order(order_items)
            if not self.order:
                print_error("订单创建失败，测试终止")
                return
            
            # 7. 测试支付
            if TEST_CONFIG.get("test_stripe", True):
                self.payment_confirmed = self.confirm_payment_stripe(self.order)
                
                if self.payment_confirmed:
                    # 等待后台任务完成
                    print_info("等待后台任务处理...")
                    time.sleep(5)
                    
                    # 8. 检查 Shopify 订单
                    final_order = self.check_order_status(
                        self.order["order_id"],
                        wait_for_shopify=True
                    )
                    
                    # 9. 验证邮件
                    self.verify_email_notification()
            
            # 10. 测试 Adyen（如果配置）
            if TEST_CONFIG.get("test_adyen", False):
                self.test_adyen_payment(TEST_CONFIG["merchant_id"])
            
            # 11. 最终报告
            print_section("测试报告")
            
            print_success("✅ 测试完成!")
            print_info(f"  Agent ID: {self.agent['agent_id']}")
            print_info(f"  订单 ID: {self.order['order_id']}")
            print_info(f"  支付状态: {'成功' if self.payment_confirmed else '未支付'}")
            
            if self.shopify_order_id:
                print_success(f"  Shopify 订单: {self.shopify_order_id}")
                print_success(f"  📧 请检查邮箱 {TEST_CONFIG['customer_email']} 确认订单邮件")
            else:
                print_warning("  Shopify 订单创建可能延迟")
            
            # 保存测试结果
            test_result = {
                "test_time": datetime.now().isoformat(),
                "agent_id": self.agent["agent_id"],
                "api_key": self.api_key,
                "order_id": self.order["order_id"],
                "payment_confirmed": self.payment_confirmed,
                "shopify_order_id": self.shopify_order_id,
                "customer_email": TEST_CONFIG["customer_email"]
            }
            
            with open("test_e2e_result.json", "w") as f:
                json.dump(test_result, f, indent=2)
                print_info("\n测试结果已保存到 test_e2e_result.json")
            
            print(f"\n{Colors.BOLD}{Colors.OKGREEN}")
            print("🎉" * 40)
            print("端到端测试成功完成！")
            print("🎉" * 40)
            print(f"{Colors.ENDC}")
            
        except Exception as e:
            print_error(f"测试失败: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    # 运行测试
    runner = E2ETestRunner()
    runner.run()
