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

