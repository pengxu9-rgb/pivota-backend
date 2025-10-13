"""
Production PSP Configuration
Real Stripe and Adyen production keys
"""

import os
from typing import Dict, Any

# Production PSP Configuration
PRODUCTION_PSP_CONFIG = {
    "stripe": {
        "api_key": os.getenv("STRIPE_SECRET_KEY", "sk_live_your_stripe_secret_key_here"),
        "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", "pk_live_your_stripe_publishable_key_here"),
        "webhook_secret": os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_your_stripe_webhook_secret_here"),
        "environment": "live",
        "routing_rules": {
            "min_amount": 0.50,
            "max_amount": 100000.0,
            "supported_currencies": ["USD", "EUR", "GBP", "CAD", "AUD"],
            "supported_countries": ["US", "CA", "GB", "DE", "FR", "AU"]
        },
        "fees": {
            "percentage": 2.9,
            "fixed": 0.30
        }
    },
    "adyen": {
        "api_key": os.getenv("ADYEN_API_KEY", "your_adyen_api_key_here"),
        "merchant_account": os.getenv("ADYEN_MERCHANT_ACCOUNT", "your_merchant_account_here"),
        "webhook_secret": os.getenv("ADYEN_WEBHOOK_SECRET", "your_adyen_webhook_secret_here"),
        "environment": "live",
        "routing_rules": {
            "min_amount": 0.50,
            "max_amount": 50000.0,
            "supported_currencies": ["USD", "EUR", "GBP", "AUD", "CAD"],
            "supported_countries": ["US", "CA", "GB", "DE", "FR", "AU"]
        },
        "fees": {
            "percentage": 1.4,
            "fixed": 0.25
        }
    }
}

def get_production_psp_config() -> Dict[str, Any]:
    """Get production PSP configuration"""
    return PRODUCTION_PSP_CONFIG

def get_stripe_config() -> Dict[str, Any]:
    """Get Stripe production configuration"""
    return PRODUCTION_PSP_CONFIG["stripe"]

def get_adyen_config() -> Dict[str, Any]:
    """Get Adyen production configuration"""
    return PRODUCTION_PSP_CONFIG["adyen"]
