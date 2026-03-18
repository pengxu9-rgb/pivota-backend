from __future__ import annotations

import os


DEFAULT_ALPHA_MERCHANT_ID = "merch_efbc46b4619cfbdf"


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
    return (os.getenv("READINESS_ALPHA_MERCHANT_ID") or DEFAULT_ALPHA_MERCHANT_ID).strip() or DEFAULT_ALPHA_MERCHANT_ID
