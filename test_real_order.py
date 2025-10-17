#!/usr/bin/env python3
"""
Real Order Test with Shopify Integration
测试真实订单流程：产品搜索 → 订单创建 → 支付 → Shopify 订单 → 邮件确认
"""

import requests
import json
import time
from datetime import datetime
from typing import Dict, Any, List, Optional


# 配置
BASE_URL = "https://web-production-fedb.up.railway.app"
# BASE_URL = "http://localhost:8000"  # 本地测试

# 测试配置 - 使用真实的邮箱地址来接收订单确认
TEST_CONFIG = {
    "merchant_id": "merch_208139f7600dbf42",  # chydantest (已连接 Stripe 和 Shopify)
    "customer_email": "peng@chydan.com",  # 您的真实邮箱，用于接收订单确认
    "test_payment": True,  # 是否测试真实支付流程
}


class Colors:
    """终端颜色"""
    HEADER = '\033[95m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_section(title: str):
    print(f"\n{Colors.HEADER}{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}{Colors.ENDC}\n")


def print_success(message: str):
    print(f"{Colors.OKGREEN}✅ {message}{Colors.ENDC}")


def print_error(message: str):
    print(f"{Colors.FAIL}❌ {message}{Colors.ENDC}")


def print_info(message: str):
    print(f"ℹ️  {message}")


def print_warning(message: str):
    print(f"{Colors.WARNING}⚠️  {message}{Colors.ENDC}")


class RealOrderTest:
    """真实订单测试"""
    
    def __init__(self):
        self.admin_token = None
        self.products = []
        self.order = None
        self.shopify_order_id = None
        
    def get_admin_token(self) -> str:
        """获取管理员 token"""
        response = requests.get(f"{BASE_URL}/auth/admin-token")
        if response.status_code == 200:
            return response.json()["token"]
        raise Exception("Failed to get admin token")
    
    def get_merchant_info(self) -> Dict[str, Any]:
        """获取商户信息"""
        print_section("步骤 1: 验证商户配置")
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.get(
            f"{BASE_URL}/merchant/onboarding/all",
            headers=headers
        )
        
        if response.status_code == 200:
            merchants = response.json().get("merchants", [])
            for merchant in merchants:
                if merchant["merchant_id"] == TEST_CONFIG["merchant_id"]:
                    print_success(f"找到商户: {merchant['business_name']}")
                    print_info(f"  商户 ID: {merchant['merchant_id']}")
                    print_info(f"  PSP 连接: {'✅' if merchant.get('psp_connected') else '❌'}")
                    print_info(f"  PSP 类型: {merchant.get('psp_type', 'N/A')}")
                    print_info(f"  MCP 连接: {'✅' if merchant.get('mcp_connected') else '❌'}")
                    print_info(f"  店铺: {merchant.get('store_url', 'N/A')}")
                    
                    if not merchant.get('psp_connected'):
                        print_error("商户未连接 PSP，无法处理支付")
                        return None
                    
                    if not merchant.get('mcp_connected'):
                        print_warning("商户未连接 Shopify，订单不会同步到店铺")
                    
                    return merchant
            
            print_error(f"未找到商户 {TEST_CONFIG['merchant_id']}")
            return None
        else:
            print_error(f"获取商户列表失败: {response.text}")
            return None
    
    def search_products(self) -> List[Dict[str, Any]]:
        """搜索产品"""
        print_section("步骤 2: 搜索产品")
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        # 使用产品 API 获取产品
        response = requests.get(
            f"{BASE_URL}/products/{TEST_CONFIG['merchant_id']}",
            params={"force_refresh": "true"},
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            products = data.get("products", [])
            print_success(f"找到 {len(products)} 个产品")
            
            # 显示前 5 个产品
            for i, product in enumerate(products[:5], 1):
                print_info(f"\n  {i}. {product.get('title', 'Unknown')}")
                print_info(f"     价格: ${product.get('price', 0)}")
                print_info(f"     库存: {'有货' if product.get('in_stock', True) else '缺货'}")
                print_info(f"     ID: {product.get('id')}")
                
                # 查找 variant 信息
                for variant in product.get("variants", []):
                    if variant.get("available", False):
                        print_info(f"     Variant ID: {variant.get('id')}")
                        print_info(f"     SKU: {variant.get('sku', 'N/A')}")
                        break
            
            return products
        else:
            print_error(f"产品搜索失败: {response.text}")
            return []
    
    def create_order(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """创建订单"""
        print_section("步骤 3: 创建订单")
        
        # 选择产品创建订单
        selected_products = []
        
        # 尝试选择有 variant 的产品
        for product in products[:3]:  # 最多选3个产品
            # 查找可用的 variant
            variant_id = None
            sku = None
            
            for variant in product.get("variants", []):
                if variant.get("available", False):
                    variant_id = str(variant.get("id"))
                    sku = variant.get("sku")
                    break
            
            item = {
                "product_id": str(product.get("id")),
                "product_title": product.get("title", "Test Product"),
                "variant_id": variant_id,
                "sku": sku,
                "quantity": 1,
                "unit_price": str(product.get("price", 10.00)),
                "subtotal": str(product.get("price", 10.00))
            }
            selected_products.append(item)
            print_info(f"  添加: {item['product_title']} x1 @ ${item['unit_price']}")
        
        if not selected_products:
            # 如果没有找到产品，创建测试产品
            print_warning("使用测试产品")
            selected_products = [
                {
                    "product_id": "test_product_1",
                    "product_title": "测试产品 A",
                    "quantity": 1,
                    "unit_price": "29.99",
                    "subtotal": "29.99"
                }
            ]
        
        # 计算总价
        total = sum(float(item["subtotal"]) for item in selected_products)
        print_info(f"\n  订单总计: ${total:.2f}")
        
        # 创建订单
        order_data = {
            "merchant_id": TEST_CONFIG["merchant_id"],
            "customer_email": TEST_CONFIG["customer_email"],
            "items": selected_products,
            "shipping_address": {
                "name": "Test Customer",
                "address_line1": "123 Main Street",
                "address_line2": "Suite 456",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
                "phone": "+14155551234"
            },
            "currency": "USD",
            "metadata": {
                "test_type": "real_order_test",
                "created_at": datetime.now().isoformat()
            }
        }
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.post(
            f"{BASE_URL}/orders/create",
            json=order_data,
            headers=headers
        )
        
        if response.status_code == 200:
            order = response.json()
            print_success(f"订单创建成功!")
            print_info(f"  订单 ID: {order['order_id']}")
            print_info(f"  总金额: ${order['total']} {order['currency']}")
            print_info(f"  状态: {order['status']}")
            
            if order.get("payment_intent_id"):
                print_info(f"  Payment Intent: {order['payment_intent_id']}")
                print_info(f"  Client Secret: {order.get('client_secret', 'N/A')[:30]}...")
            else:
                print_warning("  未生成支付意图")
            
            return order
        else:
            print_error(f"订单创建失败: {response.text}")
            return None
    
    def confirm_payment(self, order: Dict[str, Any]) -> bool:
        """确认支付"""
        print_section("步骤 4: 确认支付")
        
        if not order.get("payment_intent_id"):
            print_warning("订单没有支付意图，跳过支付")
            return False
        
        print_info("使用 Stripe 测试卡确认支付...")
        print_info("  卡号: 4242 4242 4242 4242 (Visa)")
        print_info("  过期: 任何未来日期")
        print_info("  CVV: 任意3位数")
        
        payment_data = {
            "order_id": order["order_id"],
            "payment_method_id": "pm_card_visa"  # Stripe 测试卡
        }
        
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        response = requests.post(
            f"{BASE_URL}/orders/payment/confirm",
            json=payment_data,
            headers=headers
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get("payment_status") == "succeeded":
                print_success("支付成功! 💳")
                print_info(f"  消息: {result.get('message', '')}")
                return True
            else:
                print_warning(f"支付状态: {result.get('payment_status')}")
                print_info(f"  消息: {result.get('message', '')}")
                return False
        else:
            print_error(f"支付确认失败: {response.text}")
            return False
    
    def wait_for_shopify_order(self, order_id: str) -> Optional[str]:
        """等待 Shopify 订单创建"""
        print_section("步骤 5: 等待 Shopify 订单创建")
        
        print_info("等待后台任务处理...")
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        
        max_attempts = 10
        for attempt in range(max_attempts):
            time.sleep(3)
            
            response = requests.get(
                f"{BASE_URL}/orders/{order_id}",
                headers=headers
            )
            
            if response.status_code == 200:
                order = response.json()
                print_info(f"  尝试 {attempt + 1}/{max_attempts}: 状态={order['status']}, 支付={order['payment_status']}")
                
                shopify_order_id = order.get("shopify_order_id")
                if shopify_order_id:
                    print_success(f"Shopify 订单已创建! 🛍️")
                    print_info(f"  Shopify Order ID: {shopify_order_id}")
                    return shopify_order_id
                
                if order.get("fulfillment_status"):
                    print_info(f"  履约状态: {order['fulfillment_status']}")
            else:
                print_error(f"获取订单状态失败: {response.text}")
        
        print_warning("Shopify 订单创建超时（可能还在处理中）")
        return None
    
    def verify_email(self, shopify_order_id: Optional[str]):
        """验证邮件通知"""
        print_section("步骤 6: 邮件确认")
        
        print_info(f"📧 订单确认邮件将发送到: {TEST_CONFIG['customer_email']}")
        print_info("")
        print_info("请检查您的邮箱（包括垃圾邮件文件夹）：")
        print_info("  1. Shopify 订单确认邮件")
        print_info("  2. 支付成功通知")
        
        if shopify_order_id:
            print_success(f"Shopify 订单 {shopify_order_id} 应该已发送确认邮件")
            print_info("")
            print_info("邮件应包含：")
            print_info("  ✓ 订单号和收据")
            print_info("  ✓ 商品清单和价格")
            print_info("  ✓ 配送地址")
            print_info("  ✓ 预计送达时间")
        else:
            print_warning("如果 Shopify 订单还在处理中，邮件可能会延迟几分钟")
    
    def run(self):
        """运行测试"""
        print(f"{Colors.BOLD}{Colors.HEADER}")
        print("🛒" * 40)
        print("真实订单测试（含 Shopify 集成和邮件通知）")
        print(f"测试环境: {BASE_URL}")
        print(f"客户邮箱: {TEST_CONFIG['customer_email']}")
        print("🛒" * 40)
        print(f"{Colors.ENDC}")
        
        try:
            # 1. 获取权限
            print_info("获取管理员权限...")
            self.admin_token = self.get_admin_token()
            print_success("权限获取成功")
            
            # 2. 验证商户
            merchant = self.get_merchant_info()
            if not merchant:
                print_error("商户验证失败")
                return
            
            # 3. 搜索产品
            self.products = self.search_products()
            
            # 4. 创建订单
            self.order = self.create_order(self.products)
            if not self.order:
                print_error("订单创建失败")
                return
            
            # 5. 确认支付
            if TEST_CONFIG.get("test_payment", True):
                payment_confirmed = self.confirm_payment(self.order)
                
                if payment_confirmed:
                    # 6. 等待 Shopify 订单
                    self.shopify_order_id = self.wait_for_shopify_order(self.order["order_id"])
                    
                    # 7. 验证邮件
                    self.verify_email(self.shopify_order_id)
                else:
                    print_warning("支付未成功，Shopify 订单不会创建")
            
            # 8. 最终报告
            print_section("测试完成")
            
            print_success("✅ 测试流程完成!")
            print("")
            print_info("📋 测试结果汇总：")
            print_info(f"  • Pivota 订单 ID: {self.order['order_id']}")
            print_info(f"  • 订单金额: ${self.order['total']} {self.order['currency']}")
            print_info(f"  • 支付状态: {self.order.get('payment_status', 'unpaid')}")
            
            if self.shopify_order_id:
                print_success(f"  • Shopify 订单: {self.shopify_order_id}")
                print_success(f"  • 邮件通知: 已发送到 {TEST_CONFIG['customer_email']}")
            else:
                print_warning("  • Shopify 订单: 处理中或未创建")
            
            # 保存结果
            result = {
                "test_time": datetime.now().isoformat(),
                "order_id": self.order["order_id"],
                "total": str(self.order["total"]),
                "payment_status": self.order.get("payment_status"),
                "shopify_order_id": self.shopify_order_id,
                "customer_email": TEST_CONFIG["customer_email"]
            }
            
            with open("test_real_order_result.json", "w") as f:
                json.dump(result, f, indent=2)
                print_info("\n  结果已保存到: test_real_order_result.json")
            
            print(f"\n{Colors.BOLD}{Colors.OKGREEN}")
            print("🎉" * 40)
            print("请检查邮箱确认订单邮件!")
            print("🎉" * 40)
            print(f"{Colors.ENDC}")
            
        except Exception as e:
            print_error(f"测试失败: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    test = RealOrderTest()
    test.run()
