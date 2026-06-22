from __future__ import annotations

from typing import Any, Dict, Optional

from utils.runtime_safety import env_flag


# Per-store gate for the content-writeback rung (writing AI-ready copy to an
# app-owned Shopify metafield). Mirrors services.platform_order_writeback_readiness
# exactly — same disabled/canary/enabled/paused/failed lifecycle, same fail-closed
# defaults — keyed on the PRODUCT being written instead of an order. This is the
# first write to a merchant's live store, so every unknown resolves to "no write".
GLOBAL_CONTENT_WRITEBACK_DISABLE_FLAG = "DISABLE_CONTENT_WRITEBACK"

CONTENT_WRITEBACK_STATUS_DISABLED = "disabled"
CONTENT_WRITEBACK_STATUS_CANARY = "canary"
CONTENT_WRITEBACK_STATUS_ENABLED = "enabled"
CONTENT_WRITEBACK_STATUS_PAUSED = "paused"
CONTENT_WRITEBACK_STATUS_FAILED = "failed"

CONTENT_WRITEBACK_STATUSES = {
    CONTENT_WRITEBACK_STATUS_DISABLED,
    CONTENT_WRITEBACK_STATUS_CANARY,
    CONTENT_WRITEBACK_STATUS_ENABLED,
    CONTENT_WRITEBACK_STATUS_PAUSED,
    CONTENT_WRITEBACK_STATUS_FAILED,
}

ACTIVE_STORE_STATUSES = {"active", "connected"}


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def normalize_content_writeback_status(value: Any) -> str:
    status = _clean_str(value).lower()
    if status in CONTENT_WRITEBACK_STATUSES:
        return status
    return CONTENT_WRITEBACK_STATUS_DISABLED


def normalize_store_content_writeback_readiness(
    store: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized = dict(store or {})
    normalized["content_writeback_status"] = normalize_content_writeback_status(
        normalized.get("content_writeback_status")
    )
    normalized.setdefault("content_writeback_enabled_at", None)
    normalized.setdefault("content_writeback_canary_product_id", None)
    normalized.setdefault("content_writeback_last_canary_product_id", None)
    normalized.setdefault("content_writeback_last_written_at", None)
    normalized.setdefault("content_writeback_last_error", None)
    return normalized


def store_content_writeback_context(
    store: Optional[Dict[str, Any]],
    *,
    product_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    store_present = isinstance(store, dict) and bool(store)
    normalized = normalize_store_content_writeback_readiness(store)
    expected_platform = _clean_str(platform).lower()
    store_platform = _clean_str(normalized.get("platform")).lower()
    store_status = _clean_str(normalized.get("status")).lower()
    readiness_status = normalize_content_writeback_status(
        normalized.get("content_writeback_status")
    )
    canary_product_id = _clean_str(normalized.get("content_writeback_canary_product_id"))
    current_product_id = _clean_str(product_id)

    context: Dict[str, Any] = {
        "allowed": False,
        "blocker": None,
        "global_kill_switch": GLOBAL_CONTENT_WRITEBACK_DISABLE_FLAG,
        "store_id": _clean_str(normalized.get("store_id")) or None,
        "platform": store_platform or None,
        "expected_platform": expected_platform or None,
        "store_status": store_status or None,
        "content_writeback_status": readiness_status,
        "content_writeback_canary_product_id": canary_product_id or None,
    }

    if env_flag(GLOBAL_CONTENT_WRITEBACK_DISABLE_FLAG):
        context["blocker"] = "global_content_writeback_disabled"
        return context
    if not store_present:
        context["blocker"] = "store_missing"
        return context
    if expected_platform and store_platform != expected_platform:
        context["blocker"] = "platform_mismatch"
        return context
    if store_status not in ACTIVE_STORE_STATUSES:
        context["blocker"] = "store_inactive"
        return context
    if readiness_status == CONTENT_WRITEBACK_STATUS_ENABLED:
        context["allowed"] = True
        return context
    if readiness_status == CONTENT_WRITEBACK_STATUS_CANARY:
        if not canary_product_id:
            context["blocker"] = "canary_product_missing"
            return context
        if current_product_id != canary_product_id:
            context["blocker"] = "canary_product_mismatch"
            return context
        context["allowed"] = True
        return context

    context["blocker"] = f"content_writeback_{readiness_status}"
    return context


def is_store_content_writeback_allowed(
    store: Optional[Dict[str, Any]],
    *,
    product_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> bool:
    return bool(
        store_content_writeback_context(
            store,
            product_id=product_id,
            platform=platform,
        ).get("allowed")
    )
