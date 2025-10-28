"""
BigCommerce Platform Adapter
Handles BigCommerce API integration
"""
import logging
from typing import Dict, Any, Optional, List
from decimal import Decimal
import httpx
from datetime import datetime
import hmac
import hashlib
import json

logger = logging.getLogger(__name__)


class BigCommerceAdapter:
    """BigCommerce platform adapter for store integration"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize BigCommerce adapter"""
        self.store_hash = config.get('store_hash')
        self.access_token = config.get('access_token')
        self.client_id = config.get('client_id')
        self.client_secret = config.get('client_secret')
        self.webhook_secret = config.get('webhook_secret')
        
        # API endpoints
        self.api_url = f"https://api.bigcommerce.com/stores/{self.store_hash}/v3"
        self.v2_api_url = f"https://api.bigcommerce.com/stores/{self.store_hash}/v2"
        
        self.headers = {
            'X-Auth-Token': self.access_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
    
    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate BigCommerce configuration"""
        if not self.store_hash:
            return False, "Store Hash is required"
        if not self.access_token:
            return False, "Access Token is required"
        return True, None
    
    async def test_connection(self) -> Dict[str, Any]:
        """Test BigCommerce API connection"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.v2_api_url}/store",
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return {
                        'success': True,
                        'store_name': data.get('name'),
                        'store_domain': data.get('domain'),
                        'currency': data.get('currency'),
                        'timezone': data.get('timezone', {}).get('name'),
                        'plan_name': data.get('plan_name')
                    }
                else:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                    
        except Exception as e:
            logger.error(f"BigCommerce connection test failed: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_products(
        self, 
        page: int = 1, 
        limit: int = 100,
        search: Optional[str] = None,
        category_id: Optional[int] = None,
        is_visible: bool = True
    ) -> Dict[str, Any]:
        """Get products from BigCommerce"""
        try:
            params = {
                'page': page,
                'limit': limit,
                'include': 'variants,images,custom_fields'
            }
            
            if search:
                params['keyword'] = search
            if category_id:
                params['categories:in'] = category_id
            if is_visible is not None:
                params['is_visible'] = is_visible
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/catalog/products",
                    headers=self.headers,
                    params=params,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                products = data.get('data', [])
                meta = data.get('meta', {})
                pagination = meta.get('pagination', {})
                
                # Format products for our system
                formatted_products = []
                for product in products:
                    # Get primary image
                    images = product.get('images', [])
                    primary_image = next((img['url_standard'] for img in images if img.get('is_thumbnail')), 
                                       images[0]['url_standard'] if images else None)
                    
                    formatted_products.append({
                        'id': str(product['id']),
                        'name': product['name'],
                        'description': product.get('description', ''),
                        'price': Decimal(str(product.get('price', 0))),
                        'sale_price': Decimal(str(product.get('sale_price', 0))) if product.get('sale_price') else None,
                        'sku': product.get('sku'),
                        'stock_quantity': product.get('inventory_level'),
                        'in_stock': product.get('inventory_tracking') == 'none' or product.get('inventory_level', 0) > 0,
                        'categories': [str(cat_id) for cat_id in product.get('categories', [])],
                        'images': [img['url_standard'] for img in images],
                        'primary_image': primary_image,
                        'weight': product.get('weight'),
                        'variants': product.get('variants', []),
                        'type': product.get('type'),
                        'status': 'publish' if product.get('is_visible') else 'draft',
                        'url': product.get('custom_url', {}).get('url')
                    })
                
                return {
                    'success': True,
                    'products': formatted_products,
                    'pagination': {
                        'page': pagination.get('current_page', page),
                        'per_page': pagination.get('per_page', limit),
                        'total': pagination.get('total', 0),
                        'total_pages': pagination.get('total_pages', 0)
                    }
                }
                
        except Exception as e:
            logger.error(f"BigCommerce get products error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_orders(
        self,
        page: int = 1,
        limit: int = 100,
        status_id: Optional[int] = None,
        min_date_created: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Get orders from BigCommerce"""
        try:
            params = {
                'page': page,
                'limit': limit,
                'include': 'products,shipping_addresses,coupons'
            }
            
            if status_id:
                params['status_id'] = status_id
            if min_date_created:
                params['min_date_created'] = min_date_created.strftime('%Y-%m-%d')
            
            async with httpx.AsyncClient() as client:
                # V2 API for orders
                response = await client.get(
                    f"{self.v2_api_url}/orders",
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
                
                # Get pagination from headers
                total_count = int(response.headers.get('X-Total-Count', 0))
                total_pages = int(response.headers.get('X-Total-Pages', 1))
                
                # Format orders
                formatted_orders = []
                for order in orders:
                    formatted_orders.append({
                        'id': str(order['id']),
                        'number': str(order['id']),  # BigCommerce uses ID as order number
                        'status': self._map_order_status(order.get('status_id')),
                        'total': Decimal(str(order.get('total_inc_tax', 0))),
                        'subtotal': Decimal(str(order.get('subtotal_inc_tax', 0))),
                        'currency': order.get('currency_code'),
                        'date_created': order.get('date_created'),
                        'date_modified': order.get('date_modified'),
                        'customer_id': order.get('customer_id'),
                        'billing_address': order.get('billing_address', {}),
                        'shipping_addresses': order.get('shipping_addresses', []),
                        'products': order.get('products', []),
                        'payment_method': order.get('payment_method'),
                        'payment_status': order.get('payment_status'),
                        'staff_notes': order.get('staff_notes'),
                        'customer_message': order.get('customer_message')
                    })
                
                return {
                    'success': True,
                    'orders': formatted_orders,
                    'pagination': {
                        'page': page,
                        'per_page': limit,
                        'total': total_count,
                        'total_pages': total_pages
                    }
                }
                
        except Exception as e:
            logger.error(f"BigCommerce get orders error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create an order in BigCommerce"""
        try:
            # Map our order format to BigCommerce format
            bc_order = {
                'customer_id': order_data.get('customer_id', 0),
                'billing_address': order_data.get('billing_address', {}),
                'products': [],
                'status_id': 11,  # Awaiting Fulfillment
                'payment_method': 'Pivota Payment',
                'external_source': 'Pivota'
            }
            
            # Add products
            for item in order_data.get('line_items', []):
                bc_order['products'].append({
                    'product_id': int(item.get('product_id')),
                    'quantity': item.get('quantity', 1),
                    'price_inc_tax': float(item.get('price', 0)),
                    'price_ex_tax': float(item.get('price', 0))
                })
            
            # Add shipping address if provided
            if order_data.get('shipping_address'):
                bc_order['shipping_addresses'] = [{
                    **order_data['shipping_address'],
                    'shipping_method': order_data.get('shipping_method', 'Standard')
                }]
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.v2_api_url}/orders",
                    json=bc_order,
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
                    'order_number': str(created_order['id']),
                    'total': Decimal(str(created_order.get('total_inc_tax', 0))),
                    'status': self._map_order_status(created_order.get('status_id')),
                    'order_data': created_order
                }
                
        except Exception as e:
            logger.error(f"BigCommerce create order error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def update_order_status(
        self, 
        order_id: str, 
        status: str,
        comment: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update order status in BigCommerce"""
        try:
            # Map status to BigCommerce status ID
            status_map = {
                'pending': 1,
                'shipped': 2,
                'partially_shipped': 3,
                'refunded': 4,
                'cancelled': 5,
                'declined': 6,
                'awaiting_payment': 7,
                'awaiting_pickup': 8,
                'awaiting_shipment': 9,
                'completed': 10,
                'awaiting_fulfillment': 11,
                'manual_verification_required': 12,
                'disputed': 13,
                'partially_refunded': 14
            }
            
            status_id = status_map.get(status.lower(), 11)
            
            update_data = {
                'status_id': status_id
            }
            
            if comment:
                update_data['staff_notes'] = comment
            
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    f"{self.v2_api_url}/orders/{order_id}",
                    json=update_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                return {
                    'success': True,
                    'order_id': order_id,
                    'new_status': status
                }
                
        except Exception as e:
            logger.error(f"BigCommerce update order error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def register_webhooks(self, webhook_url: str) -> Dict[str, Any]:
        """Register webhooks in BigCommerce"""
        try:
            webhooks_to_create = [
                {
                    'scope': 'store/order/created',
                    'destination': f"{webhook_url}/bigcommerce/order/created"
                },
                {
                    'scope': 'store/order/updated',
                    'destination': f"{webhook_url}/bigcommerce/order/updated"
                },
                {
                    'scope': 'store/product/updated',
                    'destination': f"{webhook_url}/bigcommerce/product/updated"
                },
                {
                    'scope': 'store/product/inventory/updated',
                    'destination': f"{webhook_url}/bigcommerce/inventory/updated"
                }
            ]
            
            created_webhooks = []
            
            async with httpx.AsyncClient() as client:
                for webhook_config in webhooks_to_create:
                    response = await client.post(
                        f"{self.api_url}/hooks",
                        json={
                            'scope': webhook_config['scope'],
                            'destination': webhook_config['destination'],
                            'is_active': True,
                            'headers': {
                                'X-Webhook-Secret': self.webhook_secret or 'pivota_webhook_secret'
                            }
                        },
                        headers=self.headers,
                        timeout=30.0
                    )
                    
                    if response.status_code in [200, 201]:
                        webhook = response.json()['data']
                        created_webhooks.append({
                            'id': webhook['id'],
                            'scope': webhook['scope'],
                            'destination': webhook['destination']
                        })
            
            return {
                'success': True,
                'webhooks': created_webhooks
            }
            
        except Exception as e:
            logger.error(f"BigCommerce webhook registration error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """Validate BigCommerce webhook signature"""
        # BigCommerce doesn't sign webhooks by default
        # Check custom header if configured
        if self.webhook_secret:
            provided_secret = headers.get('x-webhook-secret')
            return provided_secret == self.webhook_secret
        return True
    
    def _map_order_status(self, status_id: int) -> str:
        """Map BigCommerce status ID to our status"""
        status_map = {
            0: 'incomplete',
            1: 'pending',
            2: 'shipped',
            3: 'partially_shipped',
            4: 'refunded',
            5: 'cancelled',
            6: 'declined',
            7: 'awaiting_payment',
            8: 'awaiting_pickup',
            9: 'awaiting_shipment',
            10: 'completed',
            11: 'awaiting_fulfillment',
            12: 'manual_verification',
            13: 'disputed',
            14: 'partially_refunded'
        }
        return status_map.get(status_id, 'unknown')
    
    async def get_categories(self) -> Dict[str, Any]:
        """Get product categories"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/catalog/categories",
                    headers=self.headers,
                    params={'limit': 250},
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                categories = data.get('data', [])
                
                return {
                    'success': True,
                    'categories': [
                        {
                            'id': cat['id'],
                            'name': cat['name'],
                            'parent_id': cat['parent_id'],
                            'description': cat.get('description', ''),
                            'sort_order': cat.get('sort_order', 0),
                            'is_visible': cat.get('is_visible', True)
                        }
                        for cat in categories
                    ]
                }
                
        except Exception as e:
            logger.error(f"BigCommerce get categories error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_store_info(self) -> Dict[str, Any]:
        """Get detailed store information"""
        try:
            async with httpx.AsyncClient() as client:
                # Get store info
                store_response = await client.get(
                    f"{self.v2_api_url}/store",
                    headers=self.headers,
                    timeout=30.0
                )
                
                # Get payment methods
                payment_response = await client.get(
                    f"{self.v2_api_url}/payments/methods",
                    headers=self.headers,
                    timeout=30.0
                )
                
                if store_response.status_code != 200:
                    return {
                        'success': False,
                        'error': 'Failed to get store info'
                    }
                
                store_data = store_response.json()
                payment_methods = []
                
                if payment_response.status_code == 200:
                    payment_methods = payment_response.json()
                
                return {
                    'success': True,
                    'store': {
                        'id': store_data.get('id'),
                        'name': store_data.get('name'),
                        'domain': store_data.get('domain'),
                        'secure_url': store_data.get('secure_url'),
                        'currency': store_data.get('currency'),
                        'currency_symbol': store_data.get('currency_symbol'),
                        'timezone': store_data.get('timezone'),
                        'language': store_data.get('language'),
                        'plan_name': store_data.get('plan_name')
                    },
                    'payment_methods': [
                        {
                            'code': method.get('code'),
                            'name': method.get('name'),
                            'test_mode': method.get('test_mode', False)
                        }
                        for method in payment_methods
                    ]
                }
                
        except Exception as e:
            logger.error(f"BigCommerce get store info error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }

