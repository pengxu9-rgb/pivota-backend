from __future__ import annotations

import os


def _env_flag(name: str) -> bool:
    return (os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on"))


def readiness_router_enabled() -> bool:
    return any(
        (
            _env_flag("FEATURE_READINESS_AUDIT"),
            _env_flag("FEATURE_READINESS_UCP_THIN_SLICE"),
            _env_flag("FEATURE_READINESS_REAL_MERCHANT_ALPHA"),
            _env_flag("FEATURE_READINESS_SOURCE_OF_TRUTH_V1"),
            _env_flag("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA"),
        )
    )


def readiness_real_merchant_alpha_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_REAL_MERCHANT_ALPHA")


def storeless_brand_catalog_enabled() -> bool:
    """Flag: let store-less brand-authored products flow through the readiness
    pipeline + the manual create/edit endpoints. Default OFF — with it OFF the
    readiness source selector preserves the existing KeyError path, so behavior
    is byte-identical to today."""
    return _env_flag("ENABLE_STORELESS_BRAND_CATALOG")


def readiness_source_of_truth_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_SOURCE_OF_TRUTH_V1")


def readiness_canonical_checkout_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA")


def readiness_payment_bridge_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_PAYMENT_BRIDGE_ALPHA")


def readiness_payment_intent_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_PAYMENT_INTENT_ALPHA")


def readiness_payment_status_sync_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_PAYMENT_STATUS_SYNC_ALPHA")


def readiness_refund_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_REFUND_ALPHA")


def readiness_return_sync_enabled() -> bool:
    return _env_flag("FEATURE_READINESS_RETURN_SYNC_ALPHA")


def readiness_alpha_merchant_id() -> str:
    # No baked-in default: the original alpha merchant (the merch_efbc test
    # rig) was retired (#1332), and the runtime-hardcode guard forbids real
    # merchant ids in runtime files. Unset env → "" → every alpha-gated path
    # fails closed (callers all check truthiness before comparing).
    return os.getenv("READINESS_ALPHA_MERCHANT_ID", "").strip()
