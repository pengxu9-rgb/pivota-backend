"""
Feature flags for gradual rollout of new functionality
"""
import os
from typing import Dict, Any

# Feature flags with environment variable overrides
FEATURE_FLAGS = {
    # Phase 1: Internal refund processing
    "enable_internal_refund": os.getenv("FF_ENABLE_INTERNAL_REFUND", "true").lower() == "true",
    
    # Phase 2: Platform webhook refund sync
    "enable_platform_webhook_refund": os.getenv("FF_ENABLE_PLATFORM_WEBHOOK_REFUND", "false").lower() == "true",
    
    # Phase 3: Outbound platform sync (Pivota → Platform)
    "enable_platform_sync_outbound": os.getenv("FF_ENABLE_PLATFORM_SYNC_OUTBOUND", "false").lower() == "true",
    
    # Phase 3: Dispute management
    "enable_dispute_management": os.getenv("FF_ENABLE_DISPUTE_MANAGEMENT", "false").lower() == "true",
    
    # Additional flags
    "enable_refund_auto_retry": os.getenv("FF_ENABLE_REFUND_AUTO_RETRY", "true").lower() == "true",
    "enable_refund_notifications": os.getenv("FF_ENABLE_REFUND_NOTIFICATIONS", "false").lower() == "true",

    # Quote-first rollout (pricing lock)
    # When enabled, /agent/v1/orders/create requires quote_id and uses quote snapshot as source of truth.
    # Default false for staged rollout.
    "enable_quote_first_order_create": os.getenv("FF_ENABLE_QUOTE_FIRST_ORDER_CREATE", "false").lower() == "true",

    # Tiered quote-first enforcement (PCS v0.2-a):
    # - Keeps existing behavior backward-compatible by default.
    # - When enabled, we require quote_id only for merchants meeting a minimum PCS tier (e.g. L1C/L2)
    #   or on an allowlist (FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS).
    "enable_quote_first_tiered_enforcement": os.getenv("FF_ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT", "false").lower()
    == "true",

    # Transaction safety next phase. These are intentionally off until the
    # order/payment state machine is migrated and validated per merchant.
    "enable_authorization_first_orders": os.getenv("FF_ENABLE_AUTHORIZATION_FIRST_ORDERS", "false").lower()
    == "true",
    "enable_stripe_manual_capture": os.getenv("FF_ENABLE_STRIPE_MANUAL_CAPTURE", "false").lower() == "true",
    "enable_paypal_authorization_first": os.getenv("FF_ENABLE_PAYPAL_AUTHORIZATION_FIRST", "false").lower()
    == "true",
    "enable_merchant_order_failure_refund": os.getenv("FF_ENABLE_MERCHANT_ORDER_FAILURE_REFUND", "true").lower()
    == "true",
}


def is_feature_enabled(feature_name: str, context: Dict[str, Any] = None) -> bool:
    """
    Check if a feature is enabled
    
    Args:
        feature_name: Name of the feature flag
        context: Optional context for future A/B testing or gradual rollout
        
    Returns:
        bool: Whether the feature is enabled
    """
    # Default to False if feature flag doesn't exist
    return FEATURE_FLAGS.get(feature_name, False)


def get_all_flags() -> Dict[str, bool]:
    """Get all feature flags and their current states"""
    return FEATURE_FLAGS.copy()


def update_flag(feature_name: str, enabled: bool):
    """
    Update a feature flag (runtime only, doesn't persist)
    Useful for testing and dynamic feature management
    """
    if feature_name in FEATURE_FLAGS:
        FEATURE_FLAGS[feature_name] = enabled
    else:
        raise ValueError(f"Unknown feature flag: {feature_name}")


# Export commonly used flags
ENABLE_INTERNAL_REFUND = FEATURE_FLAGS["enable_internal_refund"]
ENABLE_PLATFORM_WEBHOOK_REFUND = FEATURE_FLAGS["enable_platform_webhook_refund"]
ENABLE_PLATFORM_SYNC_OUTBOUND = FEATURE_FLAGS["enable_platform_sync_outbound"]
ENABLE_DISPUTE_MANAGEMENT = FEATURE_FLAGS["enable_dispute_management"]
ENABLE_QUOTE_FIRST_ORDER_CREATE = FEATURE_FLAGS["enable_quote_first_order_create"]
ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT = FEATURE_FLAGS["enable_quote_first_tiered_enforcement"]
ENABLE_AUTHORIZATION_FIRST_ORDERS = FEATURE_FLAGS["enable_authorization_first_orders"]
ENABLE_STRIPE_MANUAL_CAPTURE = FEATURE_FLAGS["enable_stripe_manual_capture"]
ENABLE_PAYPAL_AUTHORIZATION_FIRST = FEATURE_FLAGS["enable_paypal_authorization_first"]
ENABLE_MERCHANT_ORDER_FAILURE_REFUND = FEATURE_FLAGS["enable_merchant_order_failure_refund"]
