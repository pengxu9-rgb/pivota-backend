"""
WooCommerce Platform Adapter
Handles WooCommerce REST API integration
"""
import logging
from typing import Dict, Any, Optional, List
from decimal import Decimal
import httpx
from datetime import datetime
import base64
import hmac
import hashlib

logger = logging.getLogger(__name__)


class WooCommerceAdapter:
    """WooCommerce platform adapter for store integration"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize WooCommerce adapter"""
        self.store_url = config.get('store_url', '').rstrip('/')
        self.consumer_key = config.get('consumer_key')
        self.consumer_secret = config.get('consumer_secret')
        self.webhook_secret = config.get('webhook_secret')
        self.version = config.get('api_version', 'wc/v3')
        
        # Base API URL
        self.api_url = f"{self.store_url}/wp-json/{self.version}"
        
        # Basic auth for WooCommerce REST API
        auth_string = f"{self.consumer_key}:{self.consumer_secret}"
        auth_bytes = auth_string.encode('ascii')
        auth_b64 = base64.b64encode(auth_bytes).decode('ascii')
        
        self.headers = {
            'Authorization': f'Basic {auth_b64}',
            'Content-Type': 'application/json'
        }
    
    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate WooCommerce configuration"""
        if not self.store_url:
            return False, "Store URL is required"
        if not self.consumer_key:
            return False, "Consumer Key is required"
        if not self.consumer_secret:
            return False, "Consumer Secret is required"
        return True, None
    
    async def test_connection(self) -> Dict[str, Any]:
        """Test WooCommerce API connection"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/system_status",
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return {
                        'success': True,
                        'store_name': data.get('environment', {}).get('site_title'),
                        'wc_version': data.get('environment', {}).get('version'),
                        'currency': data.get('settings', {}).get('currency'),
                        'timezone': data.get('environment', {}).get('timezone_string')
                    }
                else:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                    
        except Exception as e:
            logger.error(f"WooCommerce connection test failed: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_products(
        self, 
        page: int = 1, 
        per_page: int = 100,
        search: Optional[str] = None,
        category_id: Optional[int] = None,
        status: str = 'publish'
    ) -> Dict[str, Any]:
        """Get products from WooCommerce"""
        try:
            params = {
                'page': page,
                'per_page': per_page,
                'status': status
            }
            
            if search:
                params['search'] = search
            if category_id:
                params['category'] = category_id
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/products",
                    headers=self.headers,
                    params=params,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                products = response.json()
                total_products = int(response.headers.get('X-WP-Total', 0))
                total_pages = int(response.headers.get('X-WP-TotalPages', 0))
                
                # Format products for our system
                formatted_products = []
                for product in products:
                    formatted_products.append({
                        'id': str(product['id']),
                        'name': product['name'],
                        'description': product.get('short_description') or product.get('description'),
                        'price': Decimal(product['price'] or '0'),
                        'regular_price': Decimal(product.get('regular_price') or '0'),
                        'sale_price': Decimal(product.get('sale_price') or '0') if product.get('sale_price') else None,
                        'sku': product.get('sku'),
                        'stock_quantity': product.get('stock_quantity'),
                        'in_stock': product.get('in_stock', True),
                        'categories': [cat['name'] for cat in product.get('categories', [])],
                        'images': [img['src'] for img in product.get('images', [])],
                        'attributes': product.get('attributes', []),
                        'variations': product.get('variations', []),
                        'type': product.get('type'),
                        'status': product.get('status'),
                        'permalink': product.get('permalink')
                    })
                
                return {
                    'success': True,
                    'products': formatted_products,
                    'pagination': {
                        'page': page,
                        'per_page': per_page,
                        'total': total_products,
                        'total_pages': total_pages
                    }
                }
                
        except Exception as e:
            logger.error(f"WooCommerce get products error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_orders(
        self,
        page: int = 1,
        per_page: int = 100,
        status: Optional[str] = None,
        after: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Get orders from WooCommerce"""
        try:
            params = {
                'page': page,
                'per_page': per_page
            }
            
            if status:
                params['status'] = status
            if after:
                params['after'] = after.isoformat()
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/orders",
                    headers=self.headers,
                    params=params,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                orders = response.json()
                total_orders = int(response.headers.get('X-WP-Total', 0))
                total_pages = int(response.headers.get('X-WP-TotalPages', 0))
                
                # Format orders
                formatted_orders = []
                for order in orders:
                    formatted_orders.append({
                        'id': str(order['id']),
                        'number': order['number'],
                        'status': order['status'],
                        'total': Decimal(order['total']),
                        'currency': order['currency'],
                        'date_created': order['date_created'],
                        'date_modified': order['date_modified'],
                        'customer_id': order.get('customer_id'),
                        'billing': order.get('billing', {}),
                        'shipping': order.get('shipping', {}),
                        'line_items': order.get('line_items', []),
                        'shipping_lines': order.get('shipping_lines', []),
                        'payment_method': order.get('payment_method'),
                        'payment_method_title': order.get('payment_method_title'),
                        'meta_data': order.get('meta_data', [])
                    })
                
                return {
                    'success': True,
                    'orders': formatted_orders,
                    'pagination': {
                        'page': page,
                        'per_page': per_page,
                        'total': total_orders,
                        'total_pages': total_pages
                    }
                }
                
        except Exception as e:
            logger.error(f"WooCommerce get orders error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create an order in WooCommerce"""
        try:
            wc_order = {
                'payment_method': order_data.get('payment_method', 'pivota'),
                'payment_method_title': order_data.get('payment_method_title', 'Pivota Payment'),
                'set_paid': order_data.get('paid', False),
                'billing': order_data.get('billing', {}),
                'shipping': order_data.get('shipping', {}),
                'line_items': order_data.get('line_items', []),
                'shipping_lines': order_data.get('shipping_lines', []),
                'meta_data': order_data.get('meta_data', [])
            }
            
            if order_data.get('customer_id'):
                wc_order['customer_id'] = order_data['customer_id']
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/orders",
                    json=wc_order,
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code not in [200, 201]:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}",
                        'details': response.text
                    }
                
                created_order = response.json()
                
                return {
                    'success': True,
                    'order_id': str(created_order['id']),
                    'order_number': created_order['number'],
                    'total': Decimal(created_order['total']),
                    'status': created_order['status'],
                    'order_data': created_order
                }
                
        except Exception as e:
            logger.error(f"WooCommerce create order error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def update_order_status(
        self, 
        order_id: str, 
        status: str,
        note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update order status in WooCommerce"""
        try:
            update_data = {
                'status': status
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    f"{self.api_url}/orders/{order_id}",
                    json=update_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                # Add order note if provided
                if note:
                    await client.post(
                        f"{self.api_url}/orders/{order_id}/notes",
                        json={'note': note},
                        headers=self.headers,
                        timeout=30.0
                    )
                
                return {
                    'success': True,
                    'order_id': order_id,
                    'new_status': status
                }
                
        except Exception as e:
            logger.error(f"WooCommerce update order error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def register_webhooks(self, webhook_url: str) -> Dict[str, Any]:
        """Register webhooks in WooCommerce"""
        try:
            webhooks_to_create = [
                {
                    'name': 'Pivota Order Created',
                    'topic': 'order.created',
                    'delivery_url': f"{webhook_url}/woocommerce/order/created"
                },
                {
                    'name': 'Pivota Order Updated',
                    'topic': 'order.updated',
                    'delivery_url': f"{webhook_url}/woocommerce/order/updated"
                },
                {
                    'name': 'Pivota Product Updated',
                    'topic': 'product.updated',
                    'delivery_url': f"{webhook_url}/woocommerce/product/updated"
                }
            ]
            
            created_webhooks = []
            
            async with httpx.AsyncClient() as client:
                for webhook_config in webhooks_to_create:
                    response = await client.post(
                        f"{self.api_url}/webhooks",
                        json={
                            'name': webhook_config['name'],
                            'topic': webhook_config['topic'],
                            'delivery_url': webhook_config['delivery_url'],
                            'secret': self.webhook_secret or 'pivota_webhook_secret',
                            'status': 'active'
                        },
                        headers=self.headers,
                        timeout=30.0
                    )
                    
                    if response.status_code in [200, 201]:
                        webhook = response.json()
                        created_webhooks.append({
                            'id': webhook['id'],
                            'name': webhook['name'],
                            'topic': webhook['topic']
                        })
            
            return {
                'success': True,
                'webhooks': created_webhooks
            }
            
        except Exception as e:
            logger.error(f"WooCommerce webhook registration error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """Validate WooCommerce webhook signature"""
        signature = headers.get('x-wc-webhook-signature')
        if not signature or not self.webhook_secret:
            return False
        
        # Calculate expected signature
        expected_signature = base64.b64encode(
            hmac.new(
                self.webhook_secret.encode('utf-8'),
                body,
                hashlib.sha256
            ).digest()
        ).decode('utf-8')
        
        return hmac.compare_digest(signature, expected_signature)
    
    async def get_categories(self) -> Dict[str, Any]:
        """Get product categories"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/products/categories",
                    headers=self.headers,
                    params={'per_page': 100},
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                categories = response.json()
                
                return {
                    'success': True,
                    'categories': [
                        {
                            'id': cat['id'],
                            'name': cat['name'],
                            'slug': cat['slug'],
                            'parent': cat['parent'],
                            'description': cat['description'],
                            'count': cat['count']
                        }
                        for cat in categories
                    ]
                }
                
        except Exception as e:
            logger.error(f"WooCommerce get categories error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_payment_gateways(self) -> Dict[str, Any]:
        """Get available payment gateways"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/payment_gateways",
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                gateways = response.json()
                
                return {
                    'success': True,
                    'gateways': [
                        {
                            'id': gw['id'],
                            'title': gw['title'],
                            'enabled': gw['enabled']
                        }
                        for gw in gateways
                    ]
                }
                
        except Exception as e:
            logger.error(f"WooCommerce get payment gateways error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }