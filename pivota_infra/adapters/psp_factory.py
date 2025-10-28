"""
PSP Factory
Creates appropriate payment adapter based on PSP type
"""
import logging
from typing import Dict, Any, Optional
from .base_psp_adapter import BasePSPAdapter
# Don't import stripe_adapter - it uses different structure
from .square_adapter import SquareAdapter
from .mollie_adapter import MollieAdapter
from .braintree_adapter import BraintreeAdapter

logger = logging.getLogger(__name__)


class PSPFactory:
    """Factory for creating PSP adapters"""
    
    # Supported PSP types (only new ones, not touching existing Stripe/Adyen/PayPal/Checkout)
    PSP_ADAPTERS = {
        'square': SquareAdapter,
        'mollie': MollieAdapter,
        'braintree': BraintreeAdapter
    }
    
    @classmethod
    def create_adapter(cls, psp_type: str, config: Dict[str, Any]) -> Optional[BasePSPAdapter]:
        """
        Create a PSP adapter instance
        
        Args:
            psp_type: Type of PSP (stripe, square, mollie, braintree)
            config: PSP-specific configuration
            
        Returns:
            PSP adapter instance or None if type not supported
        """
        psp_type = psp_type.lower()
        
        if psp_type not in cls.PSP_ADAPTERS:
            logger.error(f"Unsupported PSP type: {psp_type}")
            return None
        
        try:
            adapter_class = cls.PSP_ADAPTERS[psp_type]
            adapter = adapter_class(config)
            
            # Validate configuration
            is_valid, error_msg = adapter.validate_config()
            if not is_valid:
                logger.error(f"Invalid {psp_type} configuration: {error_msg}")
                return None
            
            logger.info(f"Created {psp_type} adapter successfully")
            return adapter
            
        except Exception as e:
            logger.error(f"Error creating {psp_type} adapter: {str(e)}")
            return None
    
    @classmethod
    def get_supported_psps(cls) -> list:
        """Get list of supported PSP types"""
        return list(cls.PSP_ADAPTERS.keys())
    
    @classmethod
    def get_psp_config_schema(cls, psp_type: str) -> Dict[str, Any]:
        """Get configuration schema for a PSP type"""
        schemas = {
            'stripe': {
                'fields': [
                    {
                        'name': 'secret_key',
                        'type': 'password',
                        'required': True,
                        'label': 'Secret Key',
                        'placeholder': 'sk_test_...',
                        'description': 'Your Stripe secret key'
                    },
                    {
                        'name': 'webhook_secret',
                        'type': 'password',
                        'required': False,
                        'label': 'Webhook Secret',
                        'placeholder': 'whsec_...',
                        'description': 'Webhook endpoint secret for signature validation'
                    },
                    {
                        'name': 'test_mode',
                        'type': 'boolean',
                        'required': False,
                        'default': True,
                        'label': 'Test Mode',
                        'description': 'Use test mode keys'
                    }
                ]
            },
            'square': {
                'fields': [
                    {
                        'name': 'access_token',
                        'type': 'password',
                        'required': True,
                        'label': 'Access Token',
                        'placeholder': 'EAAAE...',
                        'description': 'Your Square access token'
                    },
                    {
                        'name': 'location_id',
                        'type': 'text',
                        'required': True,
                        'label': 'Location ID',
                        'placeholder': 'L...',
                        'description': 'Your Square location ID'
                    },
                    {
                        'name': 'application_id',
                        'type': 'text',
                        'required': False,
                        'label': 'Application ID',
                        'placeholder': 'sandbox-sq0idb-...',
                        'description': 'Your Square application ID (optional)'
                    },
                    {
                        'name': 'environment',
                        'type': 'select',
                        'required': False,
                        'default': 'sandbox',
                        'label': 'Environment',
                        'options': ['sandbox', 'production'],
                        'description': 'Square environment'
                    }
                ]
            },
            'mollie': {
                'fields': [
                    {
                        'name': 'api_key',
                        'type': 'password',
                        'required': True,
                        'label': 'API Key',
                        'placeholder': 'test_...',
                        'description': 'Your Mollie API key'
                    },
                    {
                        'name': 'profile_id',
                        'type': 'text',
                        'required': False,
                        'label': 'Profile ID',
                        'placeholder': 'pfl_...',
                        'description': 'Your Mollie profile ID (optional)'
                    },
                    {
                        'name': 'test_mode',
                        'type': 'boolean',
                        'required': False,
                        'default': True,
                        'label': 'Test Mode',
                        'description': 'Use test API key'
                    }
                ]
            },
            'braintree': {
                'fields': [
                    {
                        'name': 'merchant_id',
                        'type': 'text',
                        'required': True,
                        'label': 'Merchant ID',
                        'placeholder': 'merchant_id',
                        'description': 'Your Braintree merchant ID'
                    },
                    {
                        'name': 'public_key',
                        'type': 'text',
                        'required': True,
                        'label': 'Public Key',
                        'placeholder': 'public_key',
                        'description': 'Your Braintree public key'
                    },
                    {
                        'name': 'private_key',
                        'type': 'password',
                        'required': True,
                        'label': 'Private Key',
                        'placeholder': 'private_key',
                        'description': 'Your Braintree private key'
                    },
                    {
                        'name': 'environment',
                        'type': 'select',
                        'required': False,
                        'default': 'sandbox',
                        'label': 'Environment',
                        'options': ['sandbox', 'production'],
                        'description': 'Braintree environment'
                    }
                ]
            }
        }
        
        return schemas.get(psp_type.lower(), {})
    
    @classmethod
    def validate_psp_config(cls, psp_type: str, config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate PSP configuration without creating adapter
        
        Args:
            psp_type: Type of PSP
            config: Configuration to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        schema = cls.get_psp_config_schema(psp_type)
        if not schema:
            return False, f"Unsupported PSP type: {psp_type}"
        
        # Check required fields
        for field in schema.get('fields', []):
            if field.get('required') and not config.get(field['name']):
                return False, f"Missing required field: {field['label']}"
        
        # Try to create adapter to validate
        adapter = cls.create_adapter(psp_type, config)
        if adapter:
            return True, None
        else:
            return False, "Invalid configuration"

