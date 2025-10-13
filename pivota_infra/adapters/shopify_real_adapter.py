# adapters/shopify_real_adapter.py
"""
Shopify Real Adapter with Inventory Checking
Handles real Shopify store integration with comprehensive inventory management
"""

import asyncio
import aiohttp
import logging
import ssl
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger("shopify_real_adapter")

class ShopifyRealAdapter:
    """Real Shopify store adapter with inventory checking"""
    
    def __init__(self, shop_domain: str, access_token: str):
        # Ensure shop_domain doesn't have redundant .myshopify.com
        if shop_domain.endswith('.myshopify.com'):
            self.shop_domain = shop_domain
        else:
            self.shop_domain = f"{shop_domain}.myshopify.com"
        
        self.access_token = access_token
        self.session = None
        self.base_url = f"https://{self.shop_domain}/admin/api/2023-10"
        
        logger.info(f"Shopify adapter initialized for shop: {self.shop_domain}")

    async def __aenter__(self):
        # Create SSL context for proper certificate verification
        ssl_context = ssl.create_default_context()
        
        # Add timeout configuration to prevent hanging requests
        timeout = aiohttp.ClientTimeout(
            total=30,  # Total timeout for the entire request
            connect=10,  # Connection timeout
            sock_read=10  # Socket read timeout
        )
        
        self.session = aiohttp.ClientSession(
            headers={
                "X-Shopify-Access-Token": self.access_token,
                "Content-Type": "application/json"
            },
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            timeout=timeout
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _handle_rate_limit(self):
        """Handle Shopify rate limiting"""
        await asyncio.sleep(0.5)  # Basic rate limiting

    async def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Make HTTP request to Shopify API"""
        await self._handle_rate_limit()
        
        url = f"{self.base_url}/{endpoint}"
        
        try:
            async with self.session.request(method, url, json=data) as response:
                if response.status == 429:  # Rate limited
                    retry_after = int(response.headers.get('Retry-After', 2))
                    logger.warning(f"Rate limited, waiting {retry_after} seconds")
                    await asyncio.sleep(retry_after)
                    return await self._make_request(method, endpoint, data)
                
                response_data = await response.json()
                
                if response.status >= 400:
                    error_msg = response_data.get('errors', f"HTTP {response.status}")
                    logger.error(f"Shopify API error: {error_msg}")
                    return {"error": error_msg}
                
                return response_data
                
        except asyncio.TimeoutError:
            logger.error(f"Request timeout for {endpoint}")
            return {"error": "Request timeout"}
        except Exception as e:
            logger.error(f"Request failed for {endpoint}: {e}")
            return {"error": str(e)}

    async def get_products(self, limit: int = 250) -> Dict[str, Any]:
        """Get all products from Shopify store"""
        return await self._make_request("GET", f"products.json?limit={limit}")

    async def get_product(self, product_id: str) -> Dict[str, Any]:
        """Get specific product by ID"""
        return await self._make_request("GET", f"products/{product_id}.json")

    async def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create order in Shopify"""
        payload = {"order": order_data}
        return await self._make_request("POST", "orders.json", payload)

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get specific order by ID"""
        return await self._make_request("GET", f"orders/{order_id}.json")

    async def update_order(self, order_id: str, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Update existing order"""
        payload = {"order": order_data}
        return await self._make_request("PUT", f"orders/{order_id}.json", payload)

    async def create_customer(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create customer in Shopify"""
        payload = {"customer": customer_data}
        return await self._make_request("POST", "customers.json", payload)

    async def get_store_info(self) -> Dict[str, Any]:
        """Get store information"""
        return await self._make_request("GET", "shop.json")

    async def create_order_from_mcp(self, mcp_order: Dict[str, Any]) -> Dict[str, Any]:
        """Create Shopify order from MCP order data with inventory checking"""
        logger.info(f"Creating Shopify order for MCP order: {mcp_order.get('order_id')}")
        
        # First, get all products to find variant IDs by SKU
        products_response = await self.get_products()
        if products_response.get("error"):
            logger.error(f"Failed to fetch products: {products_response['error']}")
            return {"success": False, "error": products_response["error"]}
        
        products = products_response.get("products", [])
        sku_to_variant = {}
        
        # Build SKU to variant mapping with inventory information
        for product in products:
            for variant in product.get("variants", []):
                sku = variant.get("sku")
                if sku:
                    sku_to_variant[sku] = {
                        "variant_id": variant["id"],
                        "price": variant["price"],
                        "inventory_quantity": variant.get("inventory_quantity", 0),
                        "inventory_management": variant.get("inventory_management"),
                        "inventory_policy": variant.get("inventory_policy", "deny"),
                        "title": variant.get("title", product.get("title"))
                    }
        
        # Convert MCP order to Shopify format with inventory checking
        line_items = []
        inventory_issues = []
        
        for item in mcp_order.get("items", []):
            sku = item.get("sku")
            quantity = item.get("quantity", 1)
            
            if sku not in sku_to_variant:
                logger.error(f"SKU {sku} not found in Shopify store")
                return {"success": False, "error": f"Product with SKU {sku} not found in store"}
            
            variant_info = sku_to_variant[sku]
            
            # Check inventory levels
            available_quantity = variant_info.get("inventory_quantity", 0)
            inventory_management = variant_info.get("inventory_management")
            inventory_policy = variant_info.get("inventory_policy", "deny")
            
            # ENFORCE INVENTORY POLICY: Prevent orders for out-of-stock items with "deny" policy
            if inventory_management == "shopify" and inventory_policy == "deny":
                if available_quantity < quantity:
                    if available_quantity <= 0:
                        error_msg = f"Cannot fulfill order: {variant_info.get('title', sku)} is OUT OF STOCK (0 available, {quantity} requested). Inventory policy is set to 'deny' which prevents overselling."
                        logger.error(error_msg)
                        return {"success": False, "error": error_msg}
                    else:
                        error_msg = f"Cannot fulfill order: {variant_info.get('title', sku)} has INSUFFICIENT STOCK ({available_quantity} available, {quantity} requested). Inventory policy is set to 'deny' which prevents overselling."
                        logger.error(error_msg)
                        return {"success": False, "error": error_msg}
            
            # Use custom price if provided, otherwise use variant price
            custom_price = item.get("unit_price", variant_info["price"])
            line_item = {
                "variant_id": variant_info["variant_id"],
                "quantity": quantity,  # Use requested quantity since we've already validated inventory
                "price": str(custom_price),
                "requires_shipping": True
            }
            line_items.append(line_item)
        
        # If no items can be fulfilled, return error
        if not line_items:
            error_msg = "Order cannot be fulfilled due to inventory issues: " + "; ".join(inventory_issues)
            logger.error(error_msg)
            return {"success": False, "error": error_msg}
        
        # Log inventory warnings
        if inventory_issues:
            logger.warning(f"Inventory issues detected: {'; '.join(inventory_issues)}")
        
        # Create customer if needed
        customer_email = f"customer-{mcp_order.get('agent_id', 'unknown')}@example.com"
        customer_data = {
            "email": customer_email,
            "first_name": "MCP",
            "last_name": "Customer",
            "note": f"MCP Order: {mcp_order.get('order_id')}"
        }
        
        customer_response = await self.create_customer(customer_data)
        customer_id = None
        if not customer_response.get("error"):
            customer_id = customer_response["customer"]["id"]
        
        # Calculate totals
        subtotal = sum(float(item["price"]) * item["quantity"] for item in line_items)
        total_tax = subtotal * 0.08  # 8% tax for demo
        total_price = subtotal + total_tax
        
        # Create order data (create_order method will wrap it in {"order": ...})
        order_data = {
            "line_items": line_items,
            "customer": {"id": customer_id} if customer_id else None,
            "email": customer_email,
            "financial_status": "pending",
            "fulfillment_status": None,
            "note": f"MCP Order ID: {mcp_order.get('order_id')}",
            "tags": ["mcp-order", mcp_order.get("agent_id", "unknown")],
            "shipping_address": mcp_order.get("shipping_address", {}),
            "billing_address": mcp_order.get("shipping_address", {}),  # Use shipping as billing
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            # Explicitly set pricing
            "subtotal_price": str(subtotal),
            "total_price": str(total_price),
            "total_tax": str(total_tax),
            "currency": mcp_order.get("currency", "USD")
        }
        
        order_response = await self.create_order(order_data)
        
        if not order_response.get("error"):
            shopify_order = order_response["order"]
            logger.info(f"Created Shopify order {shopify_order['id']} for MCP order {mcp_order.get('order_id')}")
            
            result = {
                "success": True,
                "shopify_order_id": shopify_order["id"],
                "shopify_order_number": shopify_order["order_number"],
                "shopify_order": shopify_order
            }
            
            # Add inventory warnings if any
            if inventory_issues:
                result["inventory_warnings"] = inventory_issues
            
            return result
        else:
            logger.error(f"Failed to create Shopify order: {order_response['error']}")
            return {
                "success": False,
                "error": order_response["error"]
            }

    async def update_order_payment_status(self, order_id: str, payment_status: str) -> Dict[str, Any]:
        """Update order payment status in Shopify"""
        try:
            # Get current order
            order_response = await self.get_order(order_id)
            if order_response.get("error"):
                return {"success": False, "error": order_response["error"]}
            
            order = order_response["order"]
            
            # Update financial status
            update_data = {
                "financial_status": payment_status
            }
            
            update_response = await self.update_order(order_id, update_data)
            if update_response.get("error"):
                return {"success": False, "error": update_response["error"]}
            
            logger.info(f"Updated Shopify order {order_id} payment status to {payment_status}")
            return {
                "success": True,
                "order_id": order_id,
                "payment_status": payment_status,
                "updated_at": time.time()
            }
            
        except Exception as e:
            logger.error(f"Failed to update Shopify order payment status: {e}")
            return {"success": False, "error": str(e)}

    async def fulfill_order(self, order_id: str, tracking_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Fulfill order in Shopify"""
        try:
            fulfillment_data = {
                "location_id": None,  # Use default location
                "tracking_number": tracking_info.get("tracking_number") if tracking_info else None,
                "tracking_company": tracking_info.get("carrier") if tracking_info else None,
                "notify_customer": True
            }
            
            payload = {"fulfillment": fulfillment_data}
            response = await self._make_request("POST", f"orders/{order_id}/fulfillments.json", payload)
            
            if response.get("error"):
                return {"success": False, "error": response["error"]}
            
            fulfillment = response["fulfillment"]
            logger.info(f"Fulfilled Shopify order {order_id}")
            
            return {
                "success": True,
                "fulfillment_id": fulfillment["id"],
                "status": fulfillment["status"],
                "tracking_number": fulfillment.get("tracking_number"),
                "updated_at": time.time()
            }
            
        except Exception as e:
            logger.error(f"Failed to fulfill Shopify order: {e}")
            return {"success": False, "error": str(e)}

    async def get_inventory_levels(self, skus: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get inventory levels for specific SKUs"""
        try:
            products_response = await self.get_products()
            if products_response.get("error"):
                return {"error": products_response["error"]}
            
            inventory_data = {}
            products = products_response.get("products", [])
            
            for product in products:
                for variant in product.get("variants", []):
                    sku = variant.get("sku")
                    if sku in skus:
                        inventory_data[sku] = {
                            "variant_id": variant["id"],
                            "inventory_quantity": variant.get("inventory_quantity", 0),
                            "inventory_management": variant.get("inventory_management"),
                            "inventory_policy": variant.get("inventory_policy", "deny"),
                            "price": variant["price"],
                            "title": variant.get("title", product.get("title"))
                        }
            
            return inventory_data
            
        except Exception as e:
            logger.error(f"Failed to get inventory levels: {e}")
            return {"error": str(e)}

# Global registry for Shopify adapters
_shopify_adapters = {}

def register_shopify_adapter(store_name: str, shop_domain: str, access_token: str):
    """Register a Shopify adapter for a store"""
    _shopify_adapters[store_name] = {
        "shop_domain": shop_domain,
        "access_token": access_token
    }
    logger.info(f"Registered Shopify adapter for {store_name}")

def get_shopify_adapter(store_name: str) -> Optional[ShopifyRealAdapter]:
    """Get a registered Shopify adapter"""
    if store_name not in _shopify_adapters:
        return None
    
    store_info = _shopify_adapters[store_name]
    return ShopifyRealAdapter(
        shop_domain=store_info["shop_domain"],
        access_token=store_info["access_token"]
    )

def get_all_shopify_stores() -> Dict[str, Dict[str, str]]:
    """Get all registered Shopify stores"""
    return _shopify_adapters.copy()
