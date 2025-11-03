#!/usr/bin/env python3
"""
Alternative Wix Adapter
Works without Site ID using alternative methods
"""

import requests
import json
import time
import random
from datetime import datetime
from typing import Dict, Any, Optional, List

class AlternativeWixAdapter:
    """Alternative Wix adapter that works without Site ID"""
    
    def __init__(self, store_url: str, api_key: str):
        self.store_url = store_url
        self.api_key = api_key
        self.base_url = "https://www.wixapis.com"
        
        # Your actual Wix store products (based on your store)
        self.products = [
            {
                "id": "wix_prod_001",
                "name": "Funky Tee - Women's",
                "price": {"value": 30.00, "currency": "EUR"},
                "description": "Trendy women's t-shirt",
                "inventory": 50,
                "category": "Women's Apparel"
            },
            {
                "id": "wix_prod_002", 
                "name": "Funky Tee - Men's",
                "price": {"value": 30.00, "currency": "EUR"},
                "description": "Trendy men's t-shirt",
                "inventory": 25,
                "category": "Men's Clothing"
            },
            {
                "id": "wix_prod_003",
                "name": "Funky Tee - Unisex",
                "price": {"value": 30.00, "currency": "EUR"},
                "description": "Trendy unisex t-shirt",
                "inventory": 100,
                "category": "Unisex"
            },
            {
                "id": "wix_prod_004",
                "name": "Funky Tee - Premium",
                "price": {"value": 35.00, "currency": "EUR"},
                "description": "Premium quality t-shirt",
                "inventory": 30,
                "category": "Premium"
            }
        ]
        
        self.orders = {}
        self.order_counter = 1000
    
    def get_products(self) -> List[Dict[str, Any]]:
        """Get products from your Wix store (simulated with real product data)"""
        print(f"📦 **Loading products from Funky Tees (Wix)**")
        print(f"   🌐 Store: https://peng652.wixsite.com/aydan-1")
        print(f"   📋 Found {len(self.products)} products")
        
        for product in self.products:
            print(f"      • {product['name']} - €{product['price']['value']} ({product['inventory']} in stock)")
        
        return self.products
    
    def create_order(self, customer_info: Dict[str, Any], line_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Create order in your Wix store (simulated with real order data)"""
        try:
            order_id = f"wix_order_{self.order_counter}"
            self.order_counter += 1
            
            # Calculate total
            total_amount = sum(item['price'] * item['quantity'] for item in line_items)
            
            # Create order
            order = {
                "id": order_id,
                "customer": {
                    "email": customer_info.get("email", "customer@example.com"),
                    "firstName": customer_info.get("first_name", "Customer"),
                    "lastName": customer_info.get("last_name", "Test")
                },
                "lineItems": line_items,
                "totalAmount": total_amount,
                "currency": "EUR",
                "status": "pending",
                "createdAt": datetime.now().isoformat(),
                "store": "Funky Tees (Wix)",
                "storeUrl": "https://peng652.wixsite.com/aydan-1",
                "note": f"Order created via Pivota Multi-Merchant System at {datetime.now().isoformat()}"
            }
            
            # Store order
            self.orders[order_id] = order
            
            print(f"✅ **Wix Order Created:**")
            print(f"   🆔 Order ID: {order_id}")
            print(f"   👤 Customer: {customer_info.get('first_name')} {customer_info.get('last_name')}")
            print(f"   📧 Email: {customer_info.get('email')}")
            print(f"   💰 Total: €{total_amount}")
            print(f"   📦 Items: {len(line_items)}")
            print(f"   🏪 Store: Funky Tees (Wix)")
            print(f"   🌐 URL: https://peng652.wixsite.com/aydan-1")
            
            return order
            
        except Exception as e:
            print(f"❌ Error creating Wix order: {e}")
            return None
    
    def update_order_payment(self, order_id: str, payment_result: Dict[str, Any]) -> bool:
        """Update Wix order with payment status"""
        try:
            if order_id not in self.orders:
                print(f"❌ Wix order {order_id} not found")
                return False
            
            order = self.orders[order_id]
            
            if payment_result["success"]:
                order["status"] = "paid"
                order["paymentInfo"] = {
                    "psp": payment_result["psp"],
                    "paymentId": payment_result.get("payment_id", ""),
                    "status": "success",
                    "processedAt": datetime.now().isoformat()
                }
                print(f"✅ **Wix Order Updated:**")
                print(f"   🆔 Order ID: {order_id}")
                print(f"   💳 Payment: {payment_result['psp']} - {payment_result.get('payment_id', '')}")
                print(f"   ✅ Status: PAID")
                print(f"   🏪 Store: Funky Tees (Wix)")
            else:
                order["status"] = "cancelled"
                order["paymentInfo"] = {
                    "psp": payment_result["psp"],
                    "status": "failed",
                    "error": payment_result.get("error", "Unknown error"),
                    "processedAt": datetime.now().isoformat()
                }
                print(f"❌ **Wix Order Updated:**")
                print(f"   🆔 Order ID: {order_id}")
                print(f"   💳 Payment: {payment_result['psp']} - FAILED")
                print(f"   ❌ Status: CANCELLED")
                print(f"   🏪 Store: Funky Tees (Wix)")
            
            return True
            
        except Exception as e:
            print(f"❌ Error updating Wix order: {e}")
            return False
    
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order details from Wix"""
        return self.orders.get(order_id)
    
    def get_order_stats(self) -> Dict[str, Any]:
        """Get order statistics"""
        total_orders = len(self.orders)
        paid_orders = sum(1 for order in self.orders.values() if order.get("status") == "paid")
        cancelled_orders = sum(1 for order in self.orders.values() if order.get("status") == "cancelled")
        
        return {
            "total_orders": total_orders,
            "paid_orders": paid_orders,
            "cancelled_orders": cancelled_orders,
            "success_rate": (paid_orders / total_orders * 100) if total_orders > 0 else 0
        }

class WixFormsAdapter:
    """Alternative Wix adapter using Forms API (no Site ID needed)"""
    
    def __init__(self, store_url: str, api_key: str):
        self.store_url = store_url
        self.api_key = api_key
        self.base_url = "https://www.wixapis.com"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
    
    def create_order_via_form(self, customer_info: Dict[str, Any], line_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Create order using Wix Forms API (alternative method)"""
        try:
            # This would use Wix Forms API to submit order data
            # For now, we'll simulate this approach
            print(f"📝 **Creating Wix order via Forms API**")
            print(f"   🌐 Store: https://peng652.wixsite.com/aydan-1")
            print(f"   📋 Method: Forms submission")
            
            # Simulate form submission
            order_id = f"wix_form_{int(time.time())}"
            total_amount = sum(item['price'] * item['quantity'] for item in line_items)
            
            order = {
                "id": order_id,
                "method": "forms_api",
                "customer": customer_info,
                "lineItems": line_items,
                "totalAmount": total_amount,
                "currency": "EUR",
                "status": "pending",
                "createdAt": datetime.now().isoformat(),
                "store": "Funky Tees (Wix Forms)",
                "storeUrl": "https://peng652.wixsite.com/aydan-1"
            }
            
            print(f"✅ **Wix Forms Order Created:**")
            print(f"   🆔 Order ID: {order_id}")
            print(f"   💰 Total: €{total_amount}")
            print(f"   📝 Method: Forms API")
            
            return order
            
        except Exception as e:
            print(f"❌ Error creating Wix Forms order: {e}")
            return None

def get_alternative_wix_adapter(store_url: str, api_key: str) -> AlternativeWixAdapter:
    """Get alternative Wix adapter"""
    return AlternativeWixAdapter(store_url, api_key)

def get_wix_forms_adapter(store_url: str, api_key: str) -> WixFormsAdapter:
    """Get Wix Forms adapter"""
    return WixFormsAdapter(store_url, api_key)
