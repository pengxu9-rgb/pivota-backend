#!/usr/bin/env python3
"""
Wix Store Adapter
Integration with Wix stores for order creation and payment processing
"""

import requests
import json
import time
import random
from datetime import datetime
from typing import Dict, Any, Optional

class WixAdapter:
    def __init__(self, store_url: str, api_key: str):
        self.store_url = store_url
        self.api_key = api_key
        self.base_url = f"https://{store_url}/_functions"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
    
    def get_products(self) -> list:
        """Get products from Wix store"""
        try:
            # Wix API endpoint for products
            response = requests.get(f"{self.base_url}/getProducts", headers=self.headers)
            if response.status_code == 200:
                return response.json().get('products', [])
            else:
                print(f"❌ Error fetching Wix products: {response.status_code}")
                return []
        except Exception as e:
            print(f"❌ Error: {e}")
            return []
    
    def create_order(self, customer_info: Dict[str, Any], line_items: list) -> Optional[Dict[str, Any]]:
        """Create order in Wix store"""
        try:
            order_data = {
                "customer": customer_info,
                "lineItems": line_items,
                "currency": "USD",
                "status": "pending",
                "createdAt": datetime.now().isoformat(),
                "note": f"Order created via Pivota at {datetime.now().isoformat()}"
            }
            
            response = requests.post(f"{self.base_url}/createOrder", 
                                   headers=self.headers,
                                   json=order_data)
            
            if response.status_code == 201:
                order = response.json()
                print(f"✅ Wix order created: {order['id']}")
                return order
            else:
                print(f"❌ Wix order creation failed: {response.status_code}")
                print(f"   Response: {response.text}")
                return None
                
        except Exception as e:
            print(f"❌ Error creating Wix order: {e}")
            return None
    
    def update_order_payment(self, order_id: str, payment_result: Dict[str, Any]) -> bool:
        """Update Wix order with payment status"""
        try:
            if payment_result["success"]:
                status = "paid"
                note = f"Payment successful via {payment_result['psp']} - {payment_result['payment_id']}"
            else:
                status = "cancelled"
                note = f"Payment failed via {payment_result['psp']}: {payment_result.get('error', 'Unknown error')}"
            
            update_data = {
                "orderId": order_id,
                "status": status,
                "note": note,
                "paymentInfo": {
                    "psp": payment_result["psp"],
                    "paymentId": payment_result.get("payment_id", ""),
                    "status": status
                }
            }
            
            response = requests.put(f"{self.base_url}/updateOrder",
                                  headers=self.headers,
                                  json=update_data)
            
            if response.status_code == 200:
                print(f"✅ Updated Wix order {order_id}: {status}")
                return True
            else:
                print(f"❌ Failed to update Wix order: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"❌ Error updating Wix order: {e}")
            return False
    
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order details from Wix"""
        try:
            response = requests.get(f"{self.base_url}/getOrder/{order_id}", headers=self.headers)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ Error fetching Wix order: {response.status_code}")
                return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None

class WixMockAdapter:
    """Mock Wix adapter for testing without real Wix store"""
    
    def __init__(self, store_url: str = "mock-wix-store.wix.com"):
        self.store_url = store_url
        self.orders = {}
        self.products = [
            {
                "id": "wix_prod_001",
                "title": "Wix Premium T-Shirt",
                "price": 29.99,
                "currency": "USD",
                "inventory": 50
            },
            {
                "id": "wix_prod_002", 
                "title": "Wix Designer Hoodie",
                "price": 79.99,
                "currency": "USD",
                "inventory": 25
            },
            {
                "id": "wix_prod_003",
                "title": "Wix Business Card Holder",
                "price": 19.99,
                "currency": "USD",
                "inventory": 100
            }
        ]
    
    def get_products(self) -> list:
        """Get mock products"""
        print(f"📦 Wix Mock Store - {len(self.products)} products available")
        return self.products
    
    def create_order(self, customer_info: Dict[str, Any], line_items: list) -> Optional[Dict[str, Any]]:
        """Create mock order"""
        try:
            order_id = f"wix_order_{int(time.time())}"
            total_amount = sum(item['price'] * item['quantity'] for item in line_items)
            
            order = {
                "id": order_id,
                "customer": customer_info,
                "lineItems": line_items,
                "totalAmount": total_amount,
                "currency": "USD",
                "status": "pending",
                "createdAt": datetime.now().isoformat(),
                "store": "Wix Mock Store"
            }
            
            self.orders[order_id] = order
            print(f"✅ Mock Wix order created: {order_id}")
            print(f"   Total: ${total_amount}")
            print(f"   Items: {len(line_items)}")
            return order
            
        except Exception as e:
            print(f"❌ Error creating mock Wix order: {e}")
            return None
    
    def update_order_payment(self, order_id: str, payment_result: Dict[str, Any]) -> bool:
        """Update mock order with payment status"""
        try:
            if order_id in self.orders:
                if payment_result["success"]:
                    self.orders[order_id]["status"] = "paid"
                    self.orders[order_id]["paymentInfo"] = {
                        "psp": payment_result["psp"],
                        "paymentId": payment_result.get("payment_id", ""),
                        "status": "success"
                    }
                    print(f"✅ Updated mock Wix order {order_id}: paid")
                else:
                    self.orders[order_id]["status"] = "cancelled"
                    self.orders[order_id]["paymentInfo"] = {
                        "psp": payment_result["psp"],
                        "status": "failed",
                        "error": payment_result.get("error", "Unknown error")
                    }
                    print(f"✅ Updated mock Wix order {order_id}: cancelled")
                return True
            else:
                print(f"❌ Mock Wix order {order_id} not found")
                return False
        except Exception as e:
            print(f"❌ Error updating mock Wix order: {e}")
            return False
    
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get mock order details"""
        return self.orders.get(order_id)

def get_wix_adapter(store_url: str, api_key: str = None, use_mock: bool = True) -> WixAdapter:
    """Get Wix adapter instance"""
    if use_mock or not api_key:
        print("🔧 Using Wix Mock Adapter for testing")
        return WixMockAdapter(store_url)
    else:
        print("🔧 Using Real Wix Adapter")
        return WixAdapter(store_url, api_key)
