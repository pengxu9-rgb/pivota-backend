"""
PrestaShop Platform Adapter
Handles PrestaShop WebService API integration
"""
import logging
from typing import Dict, Any, Optional, List
from decimal import Decimal
import httpx
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
import base64

logger = logging.getLogger(__name__)


class PrestaShopAdapter:
    """PrestaShop platform adapter for store integration"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize PrestaShop adapter"""
        self.store_url = config.get('store_url', '').rstrip('/')
        self.api_key = config.get('api_key')
        self.debug_mode = config.get('debug_mode', False)
        
        # API endpoint
        self.api_url = f"{self.store_url}/api"
        
        # Basic auth header
        auth_string = f"{self.api_key}:"
        auth_bytes = auth_string.encode('ascii')
        auth_b64 = base64.b64encode(auth_bytes).decode('ascii')
        
        self.headers = {
            'Authorization': f'Basic {auth_b64}',
            'Content-Type': 'application/xml'
        }
        
        # JSON headers for some endpoints
        self.json_headers = {
            'Authorization': f'Basic {auth_b64}',
            'Content-Type': 'application/json',
            'Io-Format': 'JSON'
        }
    
    def validate_config(self) -> tuple[bool, Optional[str]]:
        """Validate PrestaShop configuration"""
        if not self.store_url:
            return False, "Store URL is required"
        if not self.api_key:
            return False, "API Key is required"
        return True, None
    
    async def test_connection(self) -> Dict[str, Any]:
        """Test PrestaShop API connection"""
        try:
            async with httpx.AsyncClient() as client:
                # Try to access API root
                response = await client.get(
                    self.api_url,
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    # Parse XML response
                    root = ET.fromstring(response.text)
                    
                    # Get shop info
                    shop_response = await client.get(
                        f"{self.api_url}/shops/1",
                        headers=self.json_headers,
                        timeout=30.0
                    )
                    
                    shop_data = {}
                    if shop_response.status_code == 200:
                        shop_data = shop_response.json().get('shop', {})
                    
                    return {
                        'success': True,
                        'store_name': shop_data.get('name', 'PrestaShop Store'),
                        'version': root.get('shopversion', 'Unknown'),
                        'api_version': root.get('psversion', 'Unknown')
                    }
                elif response.status_code == 401:
                    return {
                        'success': False,
                        'error': 'Invalid API key'
                    }
                else:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                    
        except Exception as e:
            logger.error(f"PrestaShop connection test failed: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_products(
        self, 
        page: int = 1, 
        limit: int = 100,
        active: Optional[bool] = True,
        category_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get products from PrestaShop"""
        try:
            # Calculate offset
            offset = (page - 1) * limit
            
            # Build filter
            filters = []
            if active is not None:
                filters.append(f"filter[active]=[{1 if active else 0}]")
            if category_id:
                filters.append(f"filter[id_category_default]=[{category_id}]")
            
            filter_string = '&'.join(filters) if filters else ''
            
            async with httpx.AsyncClient() as client:
                # Get products with display parameter
                url = f"{self.api_url}/products?display=full&limit={offset},{limit}"
                if filter_string:
                    url += f"&{filter_string}"
                
                response = await client.get(
                    url,
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                products = data.get('products', [])
                
                # Format products for our system
                formatted_products = []
                for product in products:
                    # Get stock info
                    stock_response = await client.get(
                        f"{self.api_url}/stock_availables/{product.get('id_default_combination', product['id'])}",
                        headers=self.json_headers,
                        timeout=30.0
                    )
                    
                    stock_data = {}
                    if stock_response.status_code == 200:
                        stock_data = stock_response.json().get('stock_available', {})
                    
                    # Format price
                    price = Decimal(str(product.get('price', '0')))
                    if product.get('id_tax_rules_group'):
                        # Price might be without tax, add tax if configured
                        price = price * Decimal('1.21')  # Default 21% tax
                    
                    formatted_products.append({
                        'id': str(product['id']),
                        'name': self._get_multilang_value(product.get('name', {})),
                        'description': self._get_multilang_value(product.get('description', {})),
                        'short_description': self._get_multilang_value(product.get('description_short', {})),
                        'price': price,
                        'wholesale_price': Decimal(str(product.get('wholesale_price', '0'))),
                        'reference': product.get('reference'),
                        'ean13': product.get('ean13'),
                        'stock_quantity': int(stock_data.get('quantity', 0)),
                        'in_stock': int(stock_data.get('quantity', 0)) > 0 or product.get('available_for_order') == '1',
                        'categories': self._extract_ids(product.get('associations', {}).get('categories', [])),
                        'images': self._extract_image_urls(product.get('associations', {}).get('images', [])),
                        'weight': float(product.get('weight', 0)),
                        'active': product.get('active') == '1',
                        'status': 'publish' if product.get('active') == '1' else 'draft',
                        'date_add': product.get('date_add'),
                        'date_upd': product.get('date_upd')
                    })
                
                # Get total count
                count_response = await client.get(
                    f"{self.api_url}/products?limit=1",
                    headers=self.headers,
                    timeout=30.0
                )
                
                total_products = 0
                if count_response.status_code == 200:
                    # Parse XML to get total count
                    root = ET.fromstring(count_response.text)
                    products_elem = root.find('products')
                    if products_elem is not None:
                        total_products = len(products_elem.findall('product'))
                
                return {
                    'success': True,
                    'products': formatted_products,
                    'pagination': {
                        'page': page,
                        'per_page': limit,
                        'total': total_products,
                        'total_pages': (total_products + limit - 1) // limit
                    }
                }
                
        except Exception as e:
            logger.error(f"PrestaShop get products error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_orders(
        self,
        page: int = 1,
        limit: int = 100,
        date_from: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Get orders from PrestaShop"""
        try:
            offset = (page - 1) * limit
            
            # Build filter
            filters = []
            if date_from:
                filters.append(f"filter[date_add]=[{date_from.strftime('%Y-%m-%d')},9999-12-31]")
            
            filter_string = '&'.join(filters) if filters else ''
            
            async with httpx.AsyncClient() as client:
                url = f"{self.api_url}/orders?display=full&limit={offset},{limit}&sort=[id_DESC]"
                if filter_string:
                    url += f"&{filter_string}"
                
                response = await client.get(
                    url,
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                orders = data.get('orders', [])
                
                # Format orders
                formatted_orders = []
                for order in orders:
                    # Get order details
                    order_rows = order.get('associations', {}).get('order_rows', [])
                    
                    formatted_orders.append({
                        'id': str(order['id']),
                        'reference': order.get('reference'),
                        'total_paid': Decimal(str(order.get('total_paid', '0'))),
                        'total_products': Decimal(str(order.get('total_products', '0'))),
                        'total_shipping': Decimal(str(order.get('total_shipping', '0'))),
                        'currency': order.get('currency'),
                        'payment': order.get('payment'),
                        'module': order.get('module'),
                        'date_add': order.get('date_add'),
                        'date_upd': order.get('date_upd'),
                        'current_state': order.get('current_state'),
                        'customer_id': order.get('id_customer'),
                        'carrier_id': order.get('id_carrier'),
                        'invoice_number': order.get('invoice_number'),
                        'delivery_number': order.get('delivery_number'),
                        'valid': order.get('valid') == '1',
                        'order_rows': order_rows
                    })
                
                # Get total count
                count_response = await client.get(
                    f"{self.api_url}/orders?limit=1",
                    headers=self.headers,
                    timeout=30.0
                )
                
                total_orders = 0
                if count_response.status_code == 200:
                    root = ET.fromstring(count_response.text)
                    orders_elem = root.find('orders')
                    if orders_elem is not None:
                        total_orders = len(orders_elem.findall('order'))
                
                return {
                    'success': True,
                    'orders': formatted_orders,
                    'pagination': {
                        'page': page,
                        'per_page': limit,
                        'total': total_orders,
                        'total_pages': (total_orders + limit - 1) // limit
                    }
                }
                
        except Exception as e:
            logger.error(f"PrestaShop get orders error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create an order in PrestaShop"""
        try:
            # Build PrestaShop order XML/JSON structure
            ps_order = {
                'order': {
                    'id_customer': order_data.get('customer_id', 0),
                    'id_currency': order_data.get('currency_id', 1),
                    'id_lang': order_data.get('lang_id', 1),
                    'id_carrier': order_data.get('carrier_id', 1),
                    'module': 'pivota',
                    'payment': 'Pivota Payment',
                    'total_paid': str(order_data.get('total', 0)),
                    'total_paid_real': str(order_data.get('total', 0)),
                    'total_products': str(order_data.get('subtotal', 0)),
                    'total_products_wt': str(order_data.get('subtotal', 0)),
                    'conversion_rate': '1.000000',
                    'associations': {
                        'order_rows': []
                    }
                }
            }
            
            # Add order items
            for item in order_data.get('line_items', []):
                ps_order['order']['associations']['order_rows'].append({
                    'product_id': item.get('product_id'),
                    'product_quantity': item.get('quantity', 1),
                    'product_price': str(item.get('price', 0))
                })
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/orders",
                    json=ps_order,
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code not in [200, 201]:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}",
                        'details': response.text
                    }
                
                created_order = response.json().get('order', {})
                
                return {
                    'success': True,
                    'order_id': str(created_order.get('id')),
                    'reference': created_order.get('reference'),
                    'total': Decimal(str(created_order.get('total_paid', 0))),
                    'order_data': created_order
                }
                
        except Exception as e:
            logger.error(f"PrestaShop create order error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def update_order_status(
        self, 
        order_id: str, 
        status_id: int,
        comment: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update order status in PrestaShop"""
        try:
            # Create order history entry
            order_history = {
                'order_history': {
                    'id_order': order_id,
                    'id_order_state': status_id,
                    'id_employee': 0  # API user
                }
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/order_histories",
                    json=order_history,
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code not in [200, 201]:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                return {
                    'success': True,
                    'order_id': order_id,
                    'new_status_id': status_id
                }
                
        except Exception as e:
            logger.error(f"PrestaShop update order error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_categories(self) -> Dict[str, Any]:
        """Get product categories"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/categories?display=full",
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                categories = data.get('categories', [])
                
                return {
                    'success': True,
                    'categories': [
                        {
                            'id': cat['id'],
                            'name': self._get_multilang_value(cat.get('name', {})),
                            'description': self._get_multilang_value(cat.get('description', {})),
                            'parent_id': cat.get('id_parent'),
                            'active': cat.get('active') == '1',
                            'position': cat.get('position')
                        }
                        for cat in categories
                        if cat.get('id') != '1'  # Skip root category
                    ]
                }
                
        except Exception as e:
            logger.error(f"PrestaShop get categories error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_carriers(self) -> Dict[str, Any]:
        """Get available carriers/shipping methods"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/carriers?display=full",
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                carriers = data.get('carriers', [])
                
                return {
                    'success': True,
                    'carriers': [
                        {
                            'id': carrier['id'],
                            'name': carrier.get('name'),
                            'delay': self._get_multilang_value(carrier.get('delay', {})),
                            'active': carrier.get('active') == '1',
                            'is_free': carrier.get('is_free') == '1',
                            'shipping_method': carrier.get('shipping_method'),
                            'max_weight': carrier.get('max_weight'),
                            'grade': carrier.get('grade')
                        }
                        for carrier in carriers
                        if carrier.get('active') == '1' and carrier.get('deleted') == '0'
                    ]
                }
                
        except Exception as e:
            logger.error(f"PrestaShop get carriers error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_order_states(self) -> Dict[str, Any]:
        """Get available order states"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.api_url}/order_states?display=full",
                    headers=self.json_headers,
                    timeout=30.0
                )
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': f"API returned status {response.status_code}"
                    }
                
                data = response.json()
                states = data.get('order_states', [])
                
                return {
                    'success': True,
                    'order_states': [
                        {
                            'id': state['id'],
                            'name': self._get_multilang_value(state.get('name', {})),
                            'color': state.get('color'),
                            'paid': state.get('paid') == '1',
                            'shipped': state.get('shipped') == '1',
                            'invoice': state.get('invoice') == '1',
                            'logable': state.get('logable') == '1'
                        }
                        for state in states
                        if state.get('deleted') == '0'
                    ]
                }
                
        except Exception as e:
            logger.error(f"PrestaShop get order states error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _get_multilang_value(self, multilang_dict: Dict, lang_id: int = 1) -> str:
        """Extract value from PrestaShop multilanguage field"""
        if isinstance(multilang_dict, str):
            return multilang_dict
        if isinstance(multilang_dict, dict):
            # Try to get specific language or first available
            return multilang_dict.get(str(lang_id), 
                   multilang_dict.get('1', 
                   next(iter(multilang_dict.values()), '')))
        return ''
    
    def _extract_ids(self, associations: List) -> List[str]:
        """Extract IDs from PrestaShop associations"""
        ids = []
        for item in associations:
            if isinstance(item, dict) and 'id' in item:
                ids.append(str(item['id']))
        return ids
    
    def _extract_image_urls(self, images: List) -> List[str]:
        """Build image URLs from image associations"""
        urls = []
        for image in images:
            if isinstance(image, dict) and 'id' in image:
                # PrestaShop image URL format
                urls.append(f"{self.store_url}/api/images/products/{image['id']}")
        return urls
    
    async def register_module(self) -> Dict[str, Any]:
        """Where the real telemetry module lives and how it is installed.

        PrestaShop has no API that installs a module: the merchant uploads it
        in the back office. The module Pivota ships is in the repo at
        `integrations/prestashop-module/pivotatelemetry/`; the secret it signs
        with is minted by
        `POST /integrations/prestashop/{store_id}/telemetry/ensure`.
        See docs/PRESTASHOP_TELEMETRY.md.
        """
        return {
            'success': True,
            'message': (
                'Install the Pivota telemetry module from the PrestaShop back '
                'office (Modules > Upload a module), then paste the endpoint, '
                'store id and secret into its configuration page.'
            ),
            'module_source': 'integrations/prestashop-module/pivotatelemetry/',
            'provisioning_path': '/integrations/prestashop/{store_id}/telemetry/ensure',
        }

    def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """RETIRED. Never call this; it used to return True unconditionally.

        PrestaShop sends no webhooks of its own, so there was nothing for this
        stub to verify — and returning True for every input is a fail-open
        authenticator that any caller could have been wired to by mistake. The
        real verifier is `routes/prestashop_webhooks.py::_verify_signature`,
        which checks a per-store HMAC over `timestamp + "." + body` from the
        module Pivota ships. It raises rather than returning False so a caller
        cannot mistake a refusal for a signature that merely did not match.
        """
        raise NotImplementedError(
            "PrestaShopAdapter.validate_webhook is retired; PrestaShop module "
            "deliveries are verified by routes/prestashop_webhooks.py "
            "(per-store HMAC over timestamp + '.' + body)"
        )



