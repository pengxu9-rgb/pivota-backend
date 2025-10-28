"""
E-commerce Platform Factory
Creates appropriate platform adapter based on platform type
"""
import logging
from typing import Dict, Any, Optional
from .woocommerce_adapter import WooCommerceAdapter
from .bigcommerce_adapter import BigCommerceAdapter
from .prestashop_adapter import PrestaShopAdapter

logger = logging.getLogger(__name__)


class EcommerceFactory:
    """Factory for creating e-commerce platform adapters"""
    
    # Supported platform types
    PLATFORM_ADAPTERS = {
        'woocommerce': WooCommerceAdapter,
        'bigcommerce': BigCommerceAdapter,
        'prestashop': PrestaShopAdapter,
        'shopify': None,  # Already implemented separately
    }
    
    @classmethod
    def create_adapter(cls, platform_type: str, config: Dict[str, Any]) -> Optional[Any]:
        """
        Create a platform adapter instance
        
        Args:
            platform_type: Type of platform (woocommerce, bigcommerce, prestashop, shopify)
            config: Platform-specific configuration
            
        Returns:
            Platform adapter instance or None if type not supported
        """
        platform_type = platform_type.lower()
        
        if platform_type not in cls.PLATFORM_ADAPTERS:
            logger.error(f"Unsupported platform type: {platform_type}")
            return None
        
        adapter_class = cls.PLATFORM_ADAPTERS[platform_type]
        if adapter_class is None:
            logger.error(f"Platform {platform_type} adapter not yet implemented")
            return None
        
        try:
            adapter = adapter_class(config)
            
            # Validate configuration
            is_valid, error_msg = adapter.validate_config()
            if not is_valid:
                logger.error(f"Invalid {platform_type} configuration: {error_msg}")
                return None
            
            logger.info(f"Created {platform_type} adapter successfully")
            return adapter
            
        except Exception as e:
            logger.error(f"Error creating {platform_type} adapter: {str(e)}")
            return None
    
    @classmethod
    def get_supported_platforms(cls) -> list:
        """Get list of supported platform types"""
        return [p for p, adapter in cls.PLATFORM_ADAPTERS.items() if adapter is not None]
    
    @classmethod
    def get_platform_config_schema(cls, platform_type: str) -> Dict[str, Any]:
        """Get configuration schema for a platform type"""
        schemas = {
            'woocommerce': {
                'fields': [
                    {
                        'name': 'store_url',
                        'type': 'url',
                        'required': True,
                        'label': 'Store URL',
                        'placeholder': 'https://mystore.com',
                        'description': 'Your WooCommerce store URL'
                    },
                    {
                        'name': 'consumer_key',
                        'type': 'text',
                        'required': True,
                        'label': 'Consumer Key',
                        'placeholder': 'ck_...',
                        'description': 'WooCommerce REST API consumer key'
                    },
                    {
                        'name': 'consumer_secret',
                        'type': 'password',
                        'required': True,
                        'label': 'Consumer Secret',
                        'placeholder': 'cs_...',
                        'description': 'WooCommerce REST API consumer secret'
                    },
                    {
                        'name': 'webhook_secret',
                        'type': 'password',
                        'required': False,
                        'label': 'Webhook Secret',
                        'placeholder': 'Optional webhook secret',
                        'description': 'Secret for webhook signature validation'
                    },
                    {
                        'name': 'api_version',
                        'type': 'select',
                        'required': False,
                        'default': 'wc/v3',
                        'label': 'API Version',
                        'options': ['wc/v3', 'wc/v2', 'wc/v1'],
                        'description': 'WooCommerce API version'
                    }
                ]
            },
            'bigcommerce': {
                'fields': [
                    {
                        'name': 'store_hash',
                        'type': 'text',
                        'required': True,
                        'label': 'Store Hash',
                        'placeholder': 'abc123def',
                        'description': 'Your BigCommerce store hash'
                    },
                    {
                        'name': 'access_token',
                        'type': 'password',
                        'required': True,
                        'label': 'Access Token',
                        'placeholder': 'Access token',
                        'description': 'BigCommerce API access token'
                    },
                    {
                        'name': 'client_id',
                        'type': 'text',
                        'required': False,
                        'label': 'Client ID',
                        'placeholder': 'Optional client ID',
                        'description': 'OAuth client ID (if using OAuth)'
                    },
                    {
                        'name': 'client_secret',
                        'type': 'password',
                        'required': False,
                        'label': 'Client Secret',
                        'placeholder': 'Optional client secret',
                        'description': 'OAuth client secret (if using OAuth)'
                    },
                    {
                        'name': 'webhook_secret',
                        'type': 'password',
                        'required': False,
                        'label': 'Webhook Secret',
                        'placeholder': 'Optional webhook secret',
                        'description': 'Secret for webhook validation'
                    }
                ]
            },
            'prestashop': {
                'fields': [
                    {
                        'name': 'store_url',
                        'type': 'url',
                        'required': True,
                        'label': 'Store URL',
                        'placeholder': 'https://mystore.com',
                        'description': 'Your PrestaShop store URL'
                    },
                    {
                        'name': 'api_key',
                        'type': 'password',
                        'required': True,
                        'label': 'API Key',
                        'placeholder': 'Your API key',
                        'description': 'PrestaShop WebService API key'
                    },
                    {
                        'name': 'debug_mode',
                        'type': 'boolean',
                        'required': False,
                        'default': False,
                        'label': 'Debug Mode',
                        'description': 'Enable debug mode for troubleshooting'
                    }
                ]
            },
            'shopify': {
                'fields': [
                    {
                        'name': 'shop_domain',
                        'type': 'text',
                        'required': True,
                        'label': 'Shop Domain',
                        'placeholder': 'myshop.myshopify.com',
                        'description': 'Your Shopify shop domain'
                    },
                    {
                        'name': 'access_token',
                        'type': 'password',
                        'required': True,
                        'label': 'Access Token',
                        'placeholder': 'shpat_...',
                        'description': 'Shopify private app access token'
                    },
                    {
                        'name': 'api_version',
                        'type': 'select',
                        'required': False,
                        'default': '2024-01',
                        'label': 'API Version',
                        'options': ['2024-01', '2023-10', '2023-07'],
                        'description': 'Shopify API version'
                    }
                ]
            }
        }
        
        return schemas.get(platform_type.lower(), {})
    
    @classmethod
    def validate_platform_config(cls, platform_type: str, config: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        Validate platform configuration without creating adapter
        
        Args:
            platform_type: Type of platform
            config: Configuration to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        schema = cls.get_platform_config_schema(platform_type)
        if not schema:
            return False, f"Unsupported platform type: {platform_type}"
        
        # Check required fields
        for field in schema.get('fields', []):
            if field.get('required') and not config.get(field['name']):
                return False, f"Missing required field: {field['label']}"
        
        # Try to create adapter to validate
        adapter = cls.create_adapter(platform_type, config)
        if adapter:
            return True, None
        else:
            return False, "Invalid configuration"
    
    @classmethod
    def get_platform_features(cls, platform_type: str) -> Dict[str, bool]:
        """Get feature support for a platform"""
        features = {
            'woocommerce': {
                'products': True,
                'orders': True,
                'customers': True,
                'inventory': True,
                'webhooks': True,
                'categories': True,
                'shipping': True,
                'taxes': True,
                'coupons': True,
                'refunds': True
            },
            'bigcommerce': {
                'products': True,
                'orders': True,
                'customers': True,
                'inventory': True,
                'webhooks': True,
                'categories': True,
                'shipping': True,
                'taxes': True,
                'coupons': True,
                'refunds': True,
                'abandoned_carts': True
            },
            'prestashop': {
                'products': True,
                'orders': True,
                'customers': True,
                'inventory': True,
                'webhooks': False,  # Requires module
                'categories': True,
                'shipping': True,
                'taxes': True,
                'coupons': True,
                'refunds': True,
                'multi_language': True
            },
            'shopify': {
                'products': True,
                'orders': True,
                'customers': True,
                'inventory': True,
                'webhooks': True,
                'categories': True,
                'shipping': True,
                'taxes': True,
                'coupons': True,
                'refunds': True,
                'metafields': True,
                'graphql': True
            }
        }
        
        return features.get(platform_type.lower(), {})

