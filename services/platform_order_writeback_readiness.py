from __future__ import annotations

from typing import Any, Dict, Optional

from utils.runtime_safety import env_flag


GLOBAL_ORDER_WRITEBACK_DISABLE_FLAG = "DISABLE_PLATFORM_ORDER_WRITEBACK"

ORDER_WRITEBACK_STATUS_DISABLED = "disabled"
ORDER_WRITEBACK_STATUS_CANARY = "canary"
ORDER_WRITEBACK_STATUS_ENABLED = "enabled"
ORDER_WRITEBACK_STATUS_PAUSED = "paused"
ORDER_WRITEBACK_STATUS_FAILED = "failed"

ORDER_WRITEBACK_STATUSES = {
    ORDER_WRITEBACK_STATUS_DISABLED,
    ORDER_WRITEBACK_STATUS_CANARY,
    ORDER_WRITEBACK_STATUS_ENABLED,
    ORDER_WRITEBACK_STATUS_PAUSED,
    ORDER_WRITEBACK_STATUS_FAILED,
}

ACTIVE_STORE_STATUSES = {"active", "connected"}


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def normalize_order_writeback_status(value: Any) -> str:
    status = _clean_str(value).lower()
    if status in ORDER_WRITEBACK_STATUSES:
        return status
    return ORDER_WRITEBACK_STATUS_DISABLED


def normalize_store_order_writeback_readiness(store: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = dict(store or {})
    normalized["order_writeback_status"] = normalize_order_writeback_status(
        normalized.get("order_writeback_status")
    )
    normalized.setdefault("order_writeback_enabled_at", None)
    normalized.setdefault("order_writeback_canary_order_id", None)
    normalized.setdefault("order_writeback_last_canary_order_id", None)
    normalized.setdefault("order_writeback_last_verified_at", None)
    normalized.setdefault("order_writeback_last_error", None)
    return normalized


def store_order_writeback_context(
    store: Optional[Dict[str, Any]],
    *,
    order_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    store_present = isinstance(store, dict) and bool(store)
    normalized = normalize_store_order_writeback_readiness(store)
    expected_platform = _clean_str(platform).lower()
    store_platform = _clean_str(normalized.get("platform")).lower()
    store_status = _clean_str(normalized.get("status")).lower()
    readiness_status = normalize_order_writeback_status(
        normalized.get("order_writeback_status")
    )
    canary_order_id = _clean_str(normalized.get("order_writeback_canary_order_id"))
    current_order_id = _clean_str(order_id)

    context: Dict[str, Any] = {
        "allowed": False,
        "blocker": None,
        "global_kill_switch": GLOBAL_ORDER_WRITEBACK_DISABLE_FLAG,
        "store_id": _clean_str(normalized.get("store_id")) or None,
        "platform": store_platform or None,
        "expected_platform": expected_platform or None,
        "store_status": store_status or None,
        "order_writeback_status": readiness_status,
        "order_writeback_canary_order_id": canary_order_id or None,
    }

    if env_flag(GLOBAL_ORDER_WRITEBACK_DISABLE_FLAG):
        context["blocker"] = "global_order_writeback_disabled"
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
    if readiness_status == ORDER_WRITEBACK_STATUS_ENABLED:
        context["allowed"] = True
        return context
    if readiness_status == ORDER_WRITEBACK_STATUS_CANARY:
        if not canary_order_id:
            context["blocker"] = "canary_order_missing"
            return context
        if current_order_id != canary_order_id:
            context["blocker"] = "canary_order_mismatch"
            return context
        context["allowed"] = True
        return context

    context["blocker"] = f"order_writeback_{readiness_status}"
    return context


def is_store_order_writeback_allowed(
    store: Optional[Dict[str, Any]],
    *,
    order_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> bool:
    return bool(
        store_order_writeback_context(
            store,
            order_id=order_id,
            platform=platform,
        ).get("allowed")
    )
