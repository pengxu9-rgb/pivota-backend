"""
Production PSP Configuration
Real Stripe and Adyen production keys
"""

import os
from typing import Any, Dict, Tuple

# Secrets are read from env vars with no in-code fallbacks. Previously this
# file used placeholder strings like "sk_live_your_stripe_secret_key_here"
# as os.getenv defaults, which meant a misconfigured deploy would silently
# initialise live PSP connectors with garbage keys instead of failing fast.
PRODUCTION_PSP_CONFIG: Dict[str, Dict[str, Any]] = {
    "stripe": {
        "api_key": os.getenv("STRIPE_SECRET_KEY"),
        "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY"),
        "webhook_secret": os.getenv("STRIPE_WEBHOOK_SECRET"),
        "environment": "live",
        "routing_rules": {
            "min_amount": 0.50,
            "max_amount": 100000.0,
            "supported_currencies": ["USD", "EUR", "GBP", "CAD", "AUD"],
            "supported_countries": ["US", "CA", "GB", "DE", "FR", "AU"],
        },
        "fees": {
            "percentage": 2.9,
            "fixed": 0.30,
        },
    },
    "adyen": {
        "api_key": os.getenv("ADYEN_API_KEY"),
        "merchant_account": os.getenv("ADYEN_MERCHANT_ACCOUNT"),
        "webhook_secret": os.getenv("ADYEN_WEBHOOK_SECRET"),
        "environment": "live",
        "routing_rules": {
            "min_amount": 0.50,
            "max_amount": 50000.0,
            "supported_currencies": ["USD", "EUR", "GBP", "AUD", "CAD"],
            "supported_countries": ["US", "CA", "GB", "DE", "FR", "AU"],
        },
        "fees": {
            "percentage": 1.4,
            "fixed": 0.25,
        },
    },
}

_REQUIRED_KEYS: Dict[str, Tuple[str, ...]] = {
    "stripe": ("api_key", "publishable_key", "webhook_secret"),
    "adyen": ("api_key", "merchant_account", "webhook_secret"),
}

_ENV_VAR_FOR: Dict[str, Dict[str, str]] = {
    "stripe": {
        "api_key": "STRIPE_SECRET_KEY",
        "publishable_key": "STRIPE_PUBLISHABLE_KEY",
        "webhook_secret": "STRIPE_WEBHOOK_SECRET",
    },
    "adyen": {
        "api_key": "ADYEN_API_KEY",
        "merchant_account": "ADYEN_MERCHANT_ACCOUNT",
        "webhook_secret": "ADYEN_WEBHOOK_SECRET",
    },
}


def _validate(provider: str) -> Dict[str, Any]:
    cfg = PRODUCTION_PSP_CONFIG[provider]
    missing = [
        _ENV_VAR_FOR[provider][k]
        for k in _REQUIRED_KEYS[provider]
        if not cfg.get(k)
    ]
    if missing:
        raise RuntimeError(
            f"Missing required {provider} production env vars: {missing}. "
            "Refusing to initialise live PSP connector with empty credentials."
        )
    return cfg


def get_production_psp_config() -> Dict[str, Any]:
    """Get production PSP configuration (validates both providers)."""
    _validate("stripe")
    _validate("adyen")
    return PRODUCTION_PSP_CONFIG


def get_stripe_config() -> Dict[str, Any]:
    """Get Stripe production configuration. Raises if env vars missing."""
    return _validate("stripe")


def get_adyen_config() -> Dict[str, Any]:
    """Get Adyen production configuration. Raises if env vars missing."""
    return _validate("adyen")
