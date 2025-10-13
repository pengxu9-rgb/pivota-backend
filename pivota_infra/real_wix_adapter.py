#!/usr/bin/env python3
"""
Real Wix Store Adapter
Integration with real Wix store using Wix API
"""

import requests
import json
import time
import random
from datetime import datetime
from typing import Dict, Any, Optional

class RealWixAdapter:
    def __init__(self, store_url: str, api_key: str):
        self.store_url = store_url
        self.api_key = api_key
        # Wix API endpoints
        self.base_url = f"https://www.wixapis.com"
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'wix-site-id': self._extract_site_id(store_url)
        }
    
    def _extract_site_id(self, store_url: str) -> str:
        """Extract site ID from Wix store URL"""
        # For peng652.wixsite.com/aydan-1, we need to get the site ID
        # This would typically be done through Wix API or dashboard
        # For now, we'll use a placeholder that would need to be updated
        return "your-site-id-here"
    
    def get_products(self) -> list:
        """Get products from real Wix store"""
        try:
            # Wix Stores API endpoint for products
            url = f"{self.base_url}/stores/v1/products"
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 200:
                data = response.json()
                products = data.get('products', [])
                print(f"📦 Loaded {len(products)} products from Wix store")
                return products
            else:
                print(f"❌ Error fetching Wix products: {response.status_code}")
                print(f"   Response: {response.text}")
                return []
        except Exception as e:
            print(f"❌ Error: {e}")
            return []
    
    def create_order(self, customer_info: Dict[str, Any], line_items: list) -> Optional[Dict[str, Any]]:
        """Create order in real Wix store"""
        try:
            # Wix Stores API endpoint for orders
            url = f"{self.base_url}/stores/v1/orders"
            
            order_data = {
                "customer": {
                    "email": customer_info.get("email", "customer@example.com"),
                    "firstName": customer_info.get("first_name", "Customer"),
                    "lastName": customer_info.get("last_name", "Test")
                },
                "lineItems": line_items,
                "currency": "EUR",  # Based on your store pricing
                "status": "pending",
                "createdAt": datetime.now().isoformat(),
                "note": f"Order created via Pivota at {datetime.now().isoformat()}"
            }
            
            response = requests.post(url, headers=self.headers, json=order_data)
            
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
            url = f"{self.base_url}/stores/v1/orders/{order_id}"
            
            if payment_result["success"]:
                status = "paid"
                note = f"Payment successful via {payment_result['psp']} - {payment_result['payment_id']}"
            else:
                status = "cancelled"
                note = f"Payment failed via {payment_result['psp']}: {payment_result.get('error', 'Unknown error')}"
            
            update_data = {
                "status": status,
                "note": note,
                "paymentInfo": {
                    "psp": payment_result["psp"],
                    "paymentId": payment_result.get("payment_id", ""),
                    "status": status
                }
            }
            
            response = requests.put(url, headers=self.headers, json=update_data)
            
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
            url = f"{self.base_url}/stores/v1/orders/{order_id}"
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ Error fetching Wix order: {response.status_code}")
                return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None

class WixHybridAdapter:
    """Hybrid adapter that tries real Wix API but falls back to mock for testing"""
    
    def __init__(self, store_url: str, api_key: str):
        self.store_url = store_url
        self.api_key = api_key
        self.real_adapter = RealWixAdapter(store_url, api_key)
        self.mock_products = [
            {
                "id": "wix_prod_001",
                "title": "Funky Tee - Women's",
                "price": 30.00,
                "currency": "EUR",
                "inventory": 50
            },
            {
                "id": "wix_prod_002", 
                "title": "Funky Tee - Men's",
                "price": 30.00,
                "currency": "EUR",
                "inventory": 25
            },
            {
                "id": "wix_prod_003",
                "title": "Funky Tee - Unisex",
                "price": 30.00,
                "currency": "EUR",
                "inventory": 100
            }
        ]
        self.orders = {}
    
    def get_products(self) -> list:
        """Get products - try real API first, fall back to mock"""
        try:
            # Try real Wix API first
            products = self.real_adapter.get_products()
            if products:
                return products
            else:
                print("🔧 Falling back to mock products for testing")
                return self.mock_products
        except Exception as e:
            print(f"🔧 Real Wix API failed, using mock: {e}")
            return self.mock_products
    
    def create_order(self, customer_info: Dict[str, Any], line_items: list) -> Optional[Dict[str, Any]]:
        """Create order - try real API first, fall back to mock"""
        try:
            # Try real Wix API first
            order = self.real_adapter.create_order(customer_info, line_items)
            if order:
                return order
            else:
                print("🔧 Falling back to mock order creation")
                return self._create_mock_order(customer_info, line_items)
        except Exception as e:
            print(f"🔧 Real Wix API failed, using mock: {e}")
            return self._create_mock_order(customer_info, line_items)
    
    def _create_mock_order(self, customer_info: Dict[str, Any], line_items: list) -> Dict[str, Any]:
        """Create mock order for testing"""
        order_id = f"wix_order_{int(time.time())}"
        total_amount = sum(item['price'] * item['quantity'] for item in line_items)
        
        order = {
            "id": order_id,
            "customer": customer_info,
            "lineItems": line_items,
            "totalAmount": total_amount,
            "currency": "EUR",
            "status": "pending",
            "createdAt": datetime.now().isoformat(),
            "store": "Funky Tees (Mock)"
        }
        
        self.orders[order_id] = order
        print(f"✅ Mock Wix order created: {order_id}")
        print(f"   Total: €{total_amount}")
        return order
    
    def update_order_payment(self, order_id: str, payment_result: Dict[str, Any]) -> bool:
        """Update order - try real API first, fall back to mock"""
        try:
            # Try real Wix API first
            success = self.real_adapter.update_order_payment(order_id, payment_result)
            if success:
                return True
            else:
                print("🔧 Falling back to mock order update")
                return self._update_mock_order(order_id, payment_result)
        except Exception as e:
            print(f"🔧 Real Wix API failed, using mock: {e}")
            return self._update_mock_order(order_id, payment_result)
    
    def _update_mock_order(self, order_id: str, payment_result: Dict[str, Any]) -> bool:
        """Update mock order"""
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

def get_real_wix_adapter(store_url: str, api_key: str) -> WixHybridAdapter:
    """Get real Wix adapter with fallback to mock"""
    return WixHybridAdapter(store_url, api_key)
