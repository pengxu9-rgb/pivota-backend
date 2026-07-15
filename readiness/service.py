from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import stripe

from adapters.multi_psp_orchestrator import MultiPSPOrchestrator, create_payment_with_failover
from adapters.psp_adapter import get_psp_adapter
from db.orders import create_order, get_order, mark_order_paid, update_fulfillment_info, update_order, update_payment_info
from jobs.catalog_import_worker import _get_shopify_config_for_merchant
from db.products import log_order_event
from readiness.channel_exports.acp import build_acp_export
from readiness.channel_exports.ucp import build_ucp_export
from readiness.flags import readiness_alpha_merchant_id
from readiness.models import ChannelReadinessReport, CheckoutSessionRecord, MerchantReadinessSnapshot, ReadyProduct, ReadyVariant
from readiness.order_sync import get_default_journal
from readiness.scoring import build_merchant_snapshot, find_ready_variant
from readiness.sources import load_merchant_source_dataset, supported_merchant_ids
from readiness.sync_audit import build_order_sync_audit_snapshot
from services.refund_service import refund_service
from services.merchant_store_service import get_primary_store
from services.surface_listing_registry_service import persist_channel_export
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.shopify_returns_service import (
    probe_shopify_return_eligibility_best_effort,
    sync_shopify_returns_best_effort,
)
from services.shopify_transactions_service import (
    ensure_external_payment_transaction_best_effort,
    ensure_external_refund_transaction_best_effort,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_CACHE_TTL_SECONDS = 300.0
_SNAPSHOT_CACHE: dict[str, tuple[float, MerchantReadinessSnapshot]] = {}
_SNAPSHOT_REFRESH_TASKS: dict[str, asyncio.Task[None]] = {}
_SNAPSHOT_CACHE_METRICS: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "stores": 0,
    "expired": 0,
    "stale_served": 0,
    "refreshes": 0,
    "background_refreshes": 0,
    "background_refresh_successes": 0,
    "background_refresh_failures": 0,
    "invalidations": 0,
    "invalidated_entries": 0,
}


class UnsupportedMerchantError(KeyError):
    pass


def _coerce_readiness_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _test_psp_probe_enabled() -> bool:
    return str(os.getenv("ALLOW_TEST_PSP_PROBE", "")).strip().lower() in {"1", "true", "yes", "on"}


def _test_psp_probe_merchants() -> set[str]:
    raw = os.getenv("TEST_PSP_PROBE_MERCHANTS", "") or ""
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


def _explicit_readiness_test_psp_probe_requested(
    *,
    psp_mode: Optional[str],
    test_psp_probe: Any = None,
) -> bool:
    if _coerce_readiness_bool(test_psp_probe) is True:
        return True
    return str(psp_mode or "").strip().lower() in {"test_psp_probe", "psp_test_probe"}


def _resolve_checkout_live_readiness_requirement(
    *,
    merchant_id: str,
    psp_mode: Optional[str] = None,
    test_psp_probe: Any = None,
) -> bool:
    if (
        _explicit_readiness_test_psp_probe_requested(psp_mode=psp_mode, test_psp_probe=test_psp_probe)
        and _test_psp_probe_enabled()
        and str(merchant_id or "").strip().lower() in _test_psp_probe_merchants()
    ):
        return False
    return True


def _snapshot_cache_key(merchant_id: str, channel: str) -> str:
    return f"{merchant_id}|{channel}"


def invalidate_readiness_snapshot_cache(
    merchant_id: Optional[str] = None,
    *,
    channel: Optional[str] = None,
) -> int:
    task_keys_to_cancel: List[str] = []
    for key in list(_SNAPSHOT_REFRESH_TASKS.keys()):
        cached_merchant_id, cached_channel = key.split("|", 1)
        if merchant_id is not None and cached_merchant_id != merchant_id:
            continue
        if channel is not None and cached_channel != channel:
            continue
        task_keys_to_cancel.append(key)

    for key in task_keys_to_cancel:
        task = _SNAPSHOT_REFRESH_TASKS.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    if merchant_id is None and channel is None:
        removed = len(_SNAPSHOT_CACHE)
        _SNAPSHOT_CACHE.clear()
        if removed:
            _SNAPSHOT_CACHE_METRICS["invalidations"] += 1
            _SNAPSHOT_CACHE_METRICS["invalidated_entries"] += removed
        return removed

    keys_to_drop: List[str] = []
    for key in list(_SNAPSHOT_CACHE.keys()):
        cached_merchant_id, cached_channel = key.split("|", 1)
        if merchant_id is not None and cached_merchant_id != merchant_id:
            continue
        if channel is not None and cached_channel != channel:
            continue
        keys_to_drop.append(key)

    for key in keys_to_drop:
        _SNAPSHOT_CACHE.pop(key, None)

    if keys_to_drop:
        _SNAPSHOT_CACHE_METRICS["invalidations"] += 1
        _SNAPSHOT_CACHE_METRICS["invalidated_entries"] += len(keys_to_drop)
    return len(keys_to_drop)


def get_readiness_snapshot_cache_metrics() -> Dict[str, Any]:
    total_requests = (
        _SNAPSHOT_CACHE_METRICS["hits"]
        + _SNAPSHOT_CACHE_METRICS["misses"]
        + _SNAPSHOT_CACHE_METRICS["stale_served"]
    )
    hit_rate = (_SNAPSHOT_CACHE_METRICS["hits"] / total_requests * 100.0) if total_requests else 0.0
    stale_hit_rate = (
        _SNAPSHOT_CACHE_METRICS["stale_served"] / total_requests * 100.0
    ) if total_requests else 0.0
    now_mono = time.monotonic()
    active_keys: List[Dict[str, Any]] = []
    for key, (cached_at, snapshot) in sorted(_SNAPSHOT_CACHE.items()):
        merchant_id, cached_channel = key.split("|", 1)
        age_seconds = max(0.0, now_mono - cached_at)
        active_keys.append(
            {
                "merchant_id": merchant_id,
                "channel": cached_channel,
                "generated_at": snapshot.generated_at,
                "age_seconds": round(age_seconds, 3),
                "expires_in_seconds": round(max(0.0, _SNAPSHOT_CACHE_TTL_SECONDS - age_seconds), 3),
            }
        )
    return {
        "hits": _SNAPSHOT_CACHE_METRICS["hits"],
        "misses": _SNAPSHOT_CACHE_METRICS["misses"],
        "stores": _SNAPSHOT_CACHE_METRICS["stores"],
        "expired": _SNAPSHOT_CACHE_METRICS["expired"],
        "stale_served": _SNAPSHOT_CACHE_METRICS["stale_served"],
        "refreshes": _SNAPSHOT_CACHE_METRICS["refreshes"],
        "background_refreshes": _SNAPSHOT_CACHE_METRICS["background_refreshes"],
        "background_refresh_successes": _SNAPSHOT_CACHE_METRICS["background_refresh_successes"],
        "background_refresh_failures": _SNAPSHOT_CACHE_METRICS["background_refresh_failures"],
        "invalidations": _SNAPSHOT_CACHE_METRICS["invalidations"],
        "invalidated_entries": _SNAPSHOT_CACHE_METRICS["invalidated_entries"],
        "total_requests": total_requests,
        "hit_rate": round(hit_rate, 2),
        "stale_hit_rate": round(stale_hit_rate, 2),
        "entries": len(_SNAPSHOT_CACHE),
        "refresh_tasks": sum(1 for task in _SNAPSHOT_REFRESH_TASKS.values() if not task.done()),
        "ttl_seconds": _SNAPSHOT_CACHE_TTL_SECONDS,
        "active_keys": active_keys,
    }


def reset_readiness_snapshot_cache_observability() -> None:
    _SNAPSHOT_CACHE.clear()
    for key in list(_SNAPSHOT_REFRESH_TASKS.keys()):
        task = _SNAPSHOT_REFRESH_TASKS.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
    for key in list(_SNAPSHOT_CACHE_METRICS.keys()):
        _SNAPSHOT_CACHE_METRICS[key] = 0


def _store_snapshot_cache_entry(cache_key: str, snapshot: MerchantReadinessSnapshot) -> None:
    _SNAPSHOT_CACHE[cache_key] = (
        time.monotonic(),
        snapshot.model_copy(deep=True),
    )
    _SNAPSHOT_CACHE_METRICS["stores"] += 1


async def _compute_readiness_snapshot(
    merchant_id: str,
    *,
    channel: str,
    cache_hit: bool,
) -> MerchantReadinessSnapshot:
    overall_started = time.perf_counter()
    dataset_started = time.perf_counter()
    try:
        dataset = await load_merchant_source_dataset(merchant_id)
    except KeyError as exc:
        raise UnsupportedMerchantError(merchant_id) from exc
    dataset_elapsed_ms = round((time.perf_counter() - dataset_started) * 1000.0, 2)

    snapshot_started = time.perf_counter()
    snapshot = build_merchant_snapshot(dataset, channel=channel)
    snapshot_elapsed_ms = round((time.perf_counter() - snapshot_started) * 1000.0, 2)

    logger.info(
        "readiness_snapshot_profile merchant=%s channel=%s cache_hit=%s source_dataset_load_ms=%.2f snapshot_build_ms=%.2f total_ms=%.2f product_count=%s",
        merchant_id,
        channel,
        cache_hit,
        dataset_elapsed_ms,
        snapshot_elapsed_ms,
        round((time.perf_counter() - overall_started) * 1000.0, 2),
        len(snapshot.products),
    )
    return snapshot


async def _refresh_snapshot_cache_entry(
    merchant_id: str,
    *,
    channel: str,
    cache_key: str,
    reason: str,
) -> None:
    try:
        snapshot = await _compute_readiness_snapshot(
            merchant_id,
            channel=channel,
            cache_hit=False,
        )
        _store_snapshot_cache_entry(cache_key, snapshot)
        _SNAPSHOT_CACHE_METRICS["background_refresh_successes"] += 1
        logger.info(
            "readiness_snapshot_background_refresh merchant=%s channel=%s reason=%s status=success",
            merchant_id,
            channel,
            reason,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _SNAPSHOT_CACHE_METRICS["background_refresh_failures"] += 1
        logger.warning(
            "readiness_snapshot_background_refresh merchant=%s channel=%s reason=%s status=failed error=%s",
            merchant_id,
            channel,
            reason,
            str(exc)[:200],
        )
    finally:
        task = _SNAPSHOT_REFRESH_TASKS.get(cache_key)
        if task is asyncio.current_task():
            _SNAPSHOT_REFRESH_TASKS.pop(cache_key, None)


def _schedule_snapshot_refresh(
    merchant_id: str,
    *,
    channel: str,
    cache_key: str,
    reason: str,
) -> bool:
    existing_task = _SNAPSHOT_REFRESH_TASKS.get(cache_key)
    if existing_task is not None and not existing_task.done():
        return False

    task = asyncio.create_task(
        _refresh_snapshot_cache_entry(
            merchant_id,
            channel=channel,
            cache_key=cache_key,
            reason=reason,
        )
    )
    _SNAPSHOT_REFRESH_TASKS[cache_key] = task
    _SNAPSHOT_CACHE_METRICS["background_refreshes"] += 1
    return True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalized_order_status(order_row: Optional[Dict[str, Any]]) -> str:
    if not order_row:
        return ""
    return str(order_row.get("status") or "").strip().lower()


def _normalized_payment_status(order_row: Optional[Dict[str, Any]]) -> str:
    if not order_row:
        return ""
    return str(order_row.get("payment_status") or "").strip().lower()


def _normalized_metadata(order_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not order_row:
        return {}
    metadata = order_row.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except Exception:
        return None


def _coerce_psp_status_result(result: Any) -> Tuple[bool, str, Optional[str]]:
    if isinstance(result, tuple):
        if len(result) >= 3:
            ok, status, error = result[0], result[1], result[2]
            return bool(ok), str(status or "unknown").strip().lower(), str(error) if error else None
        if len(result) == 2:
            ok, status = result
            return bool(ok), str(status or "unknown").strip().lower(), None
        if len(result) == 1:
            return True, str(result[0] or "unknown").strip().lower(), None
    if isinstance(result, str):
        status = str(result or "unknown").strip().lower()
        return status not in {"", "unknown"}, status or "unknown", None
    return False, "unknown", "Unsupported PSP status result"


def _normalize_external_payment_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"succeeded", "paid", "completed", "settled"}:
        return "paid"
    if normalized in {"processing"}:
        return "processing"
    if normalized in {"requires_payment_method", "requires_action", "requires_capture", "awaiting_payment", "pending", "unpaid", "open"}:
        return "awaiting_payment"
    if normalized in {"canceled", "cancelled"}:
        return "cancelled"
    if normalized in {"expired"}:
        return "failed"
    if normalized in {"failed", "declined"}:
        return "failed"
    return normalized or "unknown"


async def _resolve_psp_adapter_for_checkout(
    merchant_id: str,
    *,
    psp_used: Optional[str],
):
    orchestrator = MultiPSPOrchestrator(merchant_id)
    await orchestrator.load_psp_configs()
    preferred_psp = str(psp_used or "").strip().lower()

    selected = None
    if preferred_psp:
        for config in orchestrator.psp_configs:
            if str(config.psp_type or "").strip().lower() == preferred_psp:
                selected = config
                break
    if selected is None and orchestrator.psp_configs:
        selected = orchestrator.psp_configs[0]

    if selected is None:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "merchant_id": merchant_id,
                "message": "No active PSP configuration was found for this merchant.",
            }
        )

    adapter_kwargs: Dict[str, Any] = {}
    if selected.psp_type == "adyen" and selected.merchant_account:
        adapter_kwargs["merchant_account"] = selected.merchant_account
    return get_psp_adapter(selected.psp_type, selected.api_key, **adapter_kwargs), selected.psp_type


def _stripe_obj_value(obj: Any, field: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _infer_psp_from_payment_reference(payment_reference: Any) -> Optional[str]:
    ref = str(payment_reference or "").strip().lower()
    if not ref:
        return None
    if ref.startswith(("pi_", "cs_", "ch_", "src_", "seti_")):
        return "stripe"
    return None


async def _query_stripe_checkout_session_status(adapter: Any, checkout_session_id: str) -> Dict[str, Any]:
    api_key = str(getattr(adapter, "api_key", "") or "").strip()
    if not api_key:
        return {
            "ok": False,
            "raw_status": "unknown",
            "normalized_status": "unknown",
            "error": "Stripe adapter is missing an API key.",
            "psp_used": "stripe",
        }

    stripe.api_key = api_key
    session = stripe.checkout.Session.retrieve(
        checkout_session_id,
        expand=["payment_intent"],
    )
    session_payment_status = str(_stripe_obj_value(session, "payment_status") or "").strip().lower()
    session_status = str(_stripe_obj_value(session, "status") or "").strip().lower()
    payment_intent = _stripe_obj_value(session, "payment_intent")
    resolved_payment_reference = None
    resolved_client_secret = None
    payment_intent_status = ""

    if isinstance(payment_intent, str):
        resolved_payment_reference = payment_intent
    elif payment_intent is not None:
        resolved_payment_reference = str(_stripe_obj_value(payment_intent, "id") or "").strip() or None
        resolved_client_secret = str(_stripe_obj_value(payment_intent, "client_secret") or "").strip() or None
        payment_intent_status = str(_stripe_obj_value(payment_intent, "status") or "").strip().lower()

    raw_status = payment_intent_status or session_payment_status or session_status or "unknown"
    normalized_status = _normalize_external_payment_status(raw_status)
    if session_payment_status == "paid":
        normalized_status = "paid"

    return {
        "ok": True,
        "raw_status": raw_status,
        "normalized_status": normalized_status,
        "error": None,
        "psp_used": "stripe",
        "payment_reference_type": "stripe_checkout_session",
        "checkout_session_id": checkout_session_id,
        "resolved_payment_reference": resolved_payment_reference,
        "resolved_client_secret": resolved_client_secret,
        "checkout_session_status": session_status or None,
        "checkout_session_payment_status": session_payment_status or None,
    }


async def _query_payment_intent_status(
    merchant_id: str,
    *,
    payment_reference: str,
    psp_used: Optional[str],
) -> Dict[str, Any]:
    adapter, resolved_psp = await _resolve_psp_adapter_for_checkout(
        merchant_id,
        psp_used=psp_used,
    )
    if resolved_psp == "stripe" and str(payment_reference or "").strip().startswith("cs_"):
        return await _query_stripe_checkout_session_status(adapter, str(payment_reference).strip())
    result = await adapter.get_payment_status(payment_reference)
    ok, raw_status, error = _coerce_psp_status_result(result)
    normalized_status = _normalize_external_payment_status(raw_status)
    return {
        "ok": ok,
        "raw_status": raw_status,
        "normalized_status": normalized_status,
        "error": error,
        "psp_used": resolved_psp,
    }


async def _reconcile_checkout_state_from_order(
    *,
    journal,
    checkout: CheckoutSessionRecord,
    order_row: Optional[Dict[str, Any]],
) -> Optional[CheckoutSessionRecord]:
    order_status = _normalized_order_status(order_row)
    payment_status = _normalized_payment_status(order_row)
    total_refunded = float((order_row or {}).get("total_refunded") or 0)

    if order_status == "cancelled":
        await journal.append_event(
            checkout_id=checkout.checkout_id,
            event_type="merchant_cancellation_observed",
            event_payload={
                "order_id": checkout.order_id,
                "status": "cancelled",
                "payment_status": payment_status,
            },
        )
        return await journal.update_checkout_session(
            checkout.checkout_id,
            status="cancelled",
            session_payload_patch={
                "observed_order_state": {
                    "status": "cancelled",
                    "payment_status": payment_status,
                    "total_refunded": total_refunded,
                }
            },
        )

    if payment_status == "refunded":
        await journal.append_event(
            checkout_id=checkout.checkout_id,
            event_type="merchant_refund_observed",
            event_payload={
                "order_id": checkout.order_id,
                "status": "refunded",
                "payment_status": payment_status,
                "total_refunded": total_refunded,
            },
        )
        return await journal.update_checkout_session(
            checkout.checkout_id,
            status="refunded",
            session_payload_patch={
                "observed_order_state": {
                    "status": order_status or "refunded",
                    "payment_status": payment_status,
                    "total_refunded": total_refunded,
                }
            },
        )

    if payment_status == "partially_refunded" or total_refunded > 0:
        await journal.append_event(
            checkout_id=checkout.checkout_id,
            event_type="merchant_partial_refund_observed",
            event_payload={
                "order_id": checkout.order_id,
                "status": order_status or "partially_refunded",
                "payment_status": payment_status or "partially_refunded",
                "total_refunded": total_refunded,
            },
        )
        return await journal.update_checkout_session(
            checkout.checkout_id,
            status="partially_refunded",
            session_payload_patch={
                "observed_order_state": {
                    "status": order_status or "partially_refunded",
                    "payment_status": payment_status or "partially_refunded",
                    "total_refunded": total_refunded,
                }
            },
        )

    return None


def _append_sample(sample: List[str], value: Optional[str], *, sample_limit: int) -> None:
    candidate = str(value or "").strip()
    if not candidate or len(sample) >= sample_limit or candidate in sample:
        return
    sample.append(candidate)


def build_snapshot_summary_response(
    snapshot: MerchantReadinessSnapshot,
    *,
    sample_limit: int = 25,
) -> Dict[str, Any]:
    ready_variant_ids_sample: List[str] = []
    blocked_variant_ids_sample: List[str] = []
    product_ids_sample: List[str] = []
    blocked_checkout_reason_counts: Counter[str] = Counter()
    blocked_discovery_reason_counts: Counter[str] = Counter()
    products_with_reviews = 0
    grouped_products_with_reviews = 0
    total_variants = 0

    for product in snapshot.products:
        _append_sample(product_ids_sample, product.product_id, sample_limit=sample_limit)
        if product.reviews and product.reviews.has_reviews:
            products_with_reviews += 1
            if product.reviews.has_group:
                grouped_products_with_reviews += 1
        for variant in product.variants:
            total_variants += 1
            if variant.channel_coverage.get("ucp") == "ready":
                _append_sample(ready_variant_ids_sample, variant.variant_id, sample_limit=sample_limit)
            else:
                _append_sample(blocked_variant_ids_sample, variant.variant_id, sample_limit=sample_limit)
            blocked_checkout_reason_counts.update(variant.checkout.blockers)
            blocked_discovery_reason_counts.update(variant.discovery.blockers)

    return {
        "report_version": snapshot.report_version,
        "merchant_id": snapshot.merchant_id,
        "merchant_name": snapshot.merchant_name,
        "channel": snapshot.channel,
        "generated_at": snapshot.generated_at,
        "merchant_alpha_mode": snapshot.merchant_alpha_mode,
        "response_mode": "summary",
        "readiness_score": snapshot.readiness_score,
        "domain_scores": snapshot.domain_scores,
        "capability_status": snapshot.capability_status,
        "blockers": snapshot.blockers,
        "warnings": snapshot.warnings,
        "merchant_capabilities": [
            capability.model_dump() if hasattr(capability, "model_dump") else capability.dict()
            for capability in snapshot.merchant_capabilities
        ],
        "channel_coverage": [
            coverage.model_dump() if hasattr(coverage, "model_dump") else coverage.dict()
            for coverage in snapshot.channel_coverage
        ],
        "source_of_truth": snapshot.source_of_truth,
        "stubbed_capabilities": snapshot.stubbed_capabilities,
        "audit_notes": snapshot.audit_notes,
        "products": [],
        "summary": {
            "product_count": len(snapshot.products),
            "variant_count": total_variants,
            "ready_variant_count": next(
                (coverage.ready_variant_count for coverage in snapshot.channel_coverage if coverage.channel == snapshot.channel),
                0,
            ),
            "blocked_variant_count": next(
                (coverage.blocked_variant_count for coverage in snapshot.channel_coverage if coverage.channel == snapshot.channel),
                0,
            ),
            "product_ids_sample": product_ids_sample,
            "ready_variant_ids_sample": ready_variant_ids_sample,
            "blocked_variant_ids_sample": blocked_variant_ids_sample,
            "blocked_checkout_reason_counts": dict(sorted(blocked_checkout_reason_counts.items())),
            "blocked_discovery_reason_counts": dict(sorted(blocked_discovery_reason_counts.items())),
            "products_with_reviews": products_with_reviews,
            "grouped_products_with_reviews": grouped_products_with_reviews,
            "sample_limit": sample_limit,
        },
    }


def _count_servable_products_and_variants(snapshot: MerchantReadinessSnapshot, channel: str) -> Dict[str, int]:
    servable_product_ids = set()
    excluded_product_ids = set()
    servable_variant_count = 0
    excluded_variant_count = 0

    for product in snapshot.products:
        product_has_servable = False
        product_has_excluded = False
        for variant in product.variants:
            if variant.channel_coverage.get(channel) == "ready":
                servable_variant_count += 1
                product_has_servable = True
            else:
                excluded_variant_count += 1
                product_has_excluded = True
        if product_has_servable:
            servable_product_ids.add(product.product_id)
        if product_has_excluded:
            excluded_product_ids.add(product.product_id)

    return {
        "servable_product_count": len(servable_product_ids),
        "servable_variant_count": servable_variant_count,
        "excluded_product_count": len(excluded_product_ids),
        "excluded_variant_count": excluded_variant_count,
    }


def _build_visible_attribute_coverage(snapshot: MerchantReadinessSnapshot, channel: str) -> Dict[str, Any]:
    tracked_categories = ["serum", "moisturizer", "cleanser", "toner"]
    tracked_skin_concerns = ["sensitive_skin", "brightening", "hydrating"]
    tracked_formula_constraints = ["fragrance_free"]
    tracked_shade_categories = ["foundation", "lipstick", "blush", "gloss"]

    product_category_coverage: Counter[str] = Counter()
    skin_concern_coverage: Counter[str] = Counter()
    formula_constraint_coverage: Counter[str] = Counter()
    servable_product_count_by_category: Counter[str] = Counter()
    ingredient_coverage_by_category: Counter[str] = Counter()
    shade_coverage_by_category: Counter[str] = Counter()

    for product in snapshot.products:
        product_visible_attributes = dict(getattr(product, "visible_attributes", None) or {})
        product_ingredient_ids = [
            str(item or "").strip()
            for item in (getattr(product, "ingredient_ids", None) or [])
            if str(item or "").strip()
        ]
        ready_for_channel = any(variant.channel_coverage.get(channel) == "ready" for variant in product.variants)

        product_categories = [
            label
            for label in product_visible_attributes.get("product_category", [])
            if label in tracked_categories
        ]
        for label in product_categories:
            product_category_coverage.update([label])
            if ready_for_channel:
                servable_product_count_by_category.update([label])
                if product_ingredient_ids:
                    ingredient_coverage_by_category.update([label])

        for label in product_visible_attributes.get("skin_concern", []):
            if label in tracked_skin_concerns:
                skin_concern_coverage.update([label])

        for label in product_visible_attributes.get("formula_constraint", []):
            if label in tracked_formula_constraints:
                formula_constraint_coverage.update([label])

        explicit_shade_ready_variant_count = sum(
            1
            for variant in product.variants
            if variant.channel_coverage.get(channel) == "ready"
            and any(str(label or "").startswith("shade_") for label in (getattr(variant, "visible_option_labels", None) or []))
        )
        if explicit_shade_ready_variant_count > 0:
            category_blob = " ".join(
                [
                    str(getattr(product, "title", "") or "").lower(),
                    str(getattr(product, "category", "") or "").lower(),
                ]
            )
            for label in tracked_shade_categories:
                if label in category_blob:
                    shade_coverage_by_category.update([label] * explicit_shade_ready_variant_count)

    return {
        "servable_product_count_by_category": {
            label: int(servable_product_count_by_category.get(label, 0))
            for label in tracked_categories
        },
        "visible_attribute_coverage": {
            "product_category": {
                label: int(product_category_coverage.get(label, 0))
                for label in tracked_categories
            },
            "skin_concern": {
                label: int(skin_concern_coverage.get(label, 0))
                for label in tracked_skin_concerns
            },
            "formula_constraint": {
                label: int(formula_constraint_coverage.get(label, 0))
                for label in tracked_formula_constraints
            },
        },
        "ingredient_coverage_by_category": {
            label: int(ingredient_coverage_by_category.get(label, 0))
            for label in tracked_categories
        },
        "shade_coverage_by_category": {
            label: int(shade_coverage_by_category.get(label, 0))
            for label in tracked_shade_categories
        },
    }


def build_export_summary_response(
    snapshot: MerchantReadinessSnapshot,
    *,
    sample_limit: int = 25,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    export_channel = str(channel or snapshot.channel or "ucp").strip().lower() or "ucp"
    offer_ids_sample: List[str] = []
    product_ids_sample: List[str] = []
    availability_counts: Counter[str] = Counter()
    currency_counts: Counter[str] = Counter()
    review_backed_offer_count = 0
    offer_count = 0

    for product in snapshot.products:
        for variant in product.variants:
            if variant.channel_coverage.get(export_channel) != "ready":
                continue
            offer_count += 1
            _append_sample(product_ids_sample, product.product_id, sample_limit=sample_limit)
            _append_sample(
                offer_ids_sample,
                f"{export_channel}:{snapshot.merchant_id}:{product.product_id}:{variant.variant_id}",
                sample_limit=sample_limit,
            )
            availability_counts.update([str(variant.inventory.get("availability") or "unknown")])
            currency_counts.update([str(variant.price.get("currency") or "USD")])
            if variant.reviews and variant.reviews.has_reviews:
                review_backed_offer_count += 1

    readiness_score = next(
        (
            coverage.ready_variant_count * 100 // max(1, coverage.ready_variant_count + coverage.blocked_variant_count)
            for coverage in snapshot.channel_coverage
            if coverage.channel == export_channel
        ),
        0,
    )
    servable_counts = _count_servable_products_and_variants(snapshot, export_channel)
    visible_attribute_summary = _build_visible_attribute_coverage(snapshot, export_channel)
    validation_warnings = list(snapshot.warnings)
    if snapshot.capability_status.get("reviews_confidence") == "blocked":
        validation_warnings.append("review summaries are unavailable for the readiness model")
    elif review_backed_offer_count < offer_count:
        validation_warnings.append("review coverage is partial across exported offers")
    if snapshot.merchant_alpha_mode != "real_merchant_alpha":
        validation_warnings.append("checkout execution is stubbed for this thin slice")
        validation_warnings.append("merchant write-back is stubbed for this thin slice")

    return {
        "export_version": f"readiness_{export_channel}_export.v1",
        "merchant_id": snapshot.merchant_id,
        "channel": export_channel,
        "generated_at": snapshot.generated_at,
        "merchant_alpha_mode": snapshot.merchant_alpha_mode,
        "response_mode": "summary",
        "readiness_score": readiness_score,
        "capability_status": snapshot.capability_status,
        "blockers": snapshot.blockers,
        "warnings": snapshot.warnings,
        "source_of_truth": snapshot.source_of_truth,
        "validation_warnings": validation_warnings,
        "stubbed_capabilities": snapshot.stubbed_capabilities,
        **servable_counts,
        **visible_attribute_summary,
        "offers": [],
        "summary": {
            "offer_count": offer_count,
            "review_backed_offer_count": review_backed_offer_count,
            "availability_counts": dict(sorted(availability_counts.items())),
            "currency_counts": dict(sorted(currency_counts.items())),
            "offer_ids_sample": offer_ids_sample,
            "product_ids_sample": product_ids_sample,
            "sample_limit": sample_limit,
        },
    }


async def build_readiness_snapshot(
    merchant_id: str,
    channel: str = "ucp",
    *,
    force_refresh: bool = False,
) -> MerchantReadinessSnapshot:
    cache_key = _snapshot_cache_key(merchant_id, channel)
    if force_refresh:
        invalidate_readiness_snapshot_cache(merchant_id, channel=channel)
        _SNAPSHOT_CACHE_METRICS["refreshes"] += 1

    cached_entry = _SNAPSHOT_CACHE.get(cache_key)
    if cached_entry is not None:
        cached_at, cached_snapshot = cached_entry
        age_seconds = time.monotonic() - cached_at
        if age_seconds <= _SNAPSHOT_CACHE_TTL_SECONDS:
            _SNAPSHOT_CACHE_METRICS["hits"] += 1
            return cached_snapshot.model_copy(deep=True)
        _SNAPSHOT_CACHE_METRICS["expired"] += 1
        _SNAPSHOT_CACHE_METRICS["stale_served"] += 1
        _schedule_snapshot_refresh(
            merchant_id,
            channel=channel,
            cache_key=cache_key,
            reason="ttl_expired",
        )
        return cached_snapshot.model_copy(deep=True)

    _SNAPSHOT_CACHE_METRICS["misses"] += 1
    snapshot = await _compute_readiness_snapshot(
        merchant_id,
        channel=channel,
        cache_hit=False,
    )
    _store_snapshot_cache_entry(cache_key, snapshot)
    return snapshot.model_copy(deep=True)


async def build_channel_export(merchant_id: str, channel: str = "ucp") -> ChannelReadinessReport:
    snapshot = await build_readiness_snapshot(merchant_id, channel=channel)
    report: ChannelReadinessReport
    if channel == "ucp":
        report = build_ucp_export(snapshot)
    elif channel == "acp":
        report = build_acp_export(snapshot)
    elif channel not in {"ucp", "acp"}:
        raise ValueError(f"Unsupported channel export: {channel}")
    else:
        raise ValueError(f"Unsupported channel export: {channel}")

    try:
        await persist_channel_export(snapshot, report)
    except Exception as exc:
        logger.warning(
            "Failed to persist surface listing registry for merchant=%s channel=%s: %s",
            merchant_id,
            channel,
            exc,
        )
    return report


def supported_merchants() -> list[str]:
    return supported_merchant_ids()


async def resolve_snapshot_variant(
    merchant_id: str,
    variant_id: str,
    channel: str = "ucp",
) -> Tuple[MerchantReadinessSnapshot, ReadyProduct, ReadyVariant]:
    snapshot = await build_readiness_snapshot(merchant_id, channel=channel)
    product, variant = find_ready_variant(snapshot, variant_id)
    if product is None or variant is None:
        raise KeyError(variant_id)
    return snapshot, product, variant


async def create_checkout_session(
    *,
    merchant_id: str,
    variant_id: str,
    quantity: int,
    base_url: str,
    idempotency_key: Optional[str] = None,
    buyer_email: Optional[str] = None,
    customer_name: Optional[str] = None,
    shipping_address: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot, product, variant = await resolve_snapshot_variant(merchant_id, variant_id, channel="ucp")
    dataset = await load_merchant_source_dataset(merchant_id)
    if variant.channel_coverage.get("ucp") != "ready":
        raise ValueError(
            {
                "code": "VARIANT_NOT_READY_FOR_CHECKOUT",
                "variant_id": variant_id,
                "blockers": variant.checkout.blockers,
                "warnings": variant.checkout.warnings,
            }
        )

    payment_mode = "merchant_native_alpha" if snapshot.merchant_alpha_mode == "real_merchant_alpha" else "stubbed"
    journal = get_default_journal()
    session_payload = {
        "merchant_alpha_mode": snapshot.merchant_alpha_mode,
        "merchant_name": snapshot.merchant_name,
        "product_id": product.product_id,
        "product_title": product.title,
        "variant_id": variant.variant_id,
        "variant_title": variant.title,
        "quantity": quantity,
        "price": variant.price,
        "inventory": variant.inventory,
        "channel": "ucp",
        "source_of_truth": {family: decision.source for family, decision in variant.source_of_truth.items()},
        "capability_status": snapshot.capability_status,
        "buyer_email": buyer_email,
        "customer_name": customer_name,
        "shipping_address": shipping_address,
        "merchant_connection": dataset.merchant_connection,
        "payment_capabilities": dataset.payment_capabilities,
    }
    checkout = await journal.create_checkout_session(
        merchant_id=merchant_id,
        channel="ucp",
        variant_id=variant.variant_id,
        quantity=quantity,
        payment_mode=payment_mode,
        session_payload=session_payload,
        continue_url=f"{base_url}/internal/readiness/checkout-sessions/{{checkout_id}}",
        idempotency_key=idempotency_key,
    )
    continue_url = checkout.continue_url.format(checkout_id=checkout.checkout_id) if checkout.continue_url else None
    warnings = list(snapshot.warnings)
    if snapshot.merchant_alpha_mode != "real_merchant_alpha":
        warnings.extend(["payment execution is stubbed", "merchant write-back is stubbed"])
    if snapshot.merchant_alpha_mode == "real_merchant_alpha" and (not buyer_email or not shipping_address):
        warnings.append("buyer_context_incomplete_for_order_writeback")
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": snapshot.merchant_alpha_mode,
        "checkout_id": checkout.checkout_id,
        "session_handle": checkout.checkout_id,
        "variant_id": checkout.variant_id,
        "quantity": checkout.quantity,
        "payment_mode": checkout.payment_mode,
        "status": checkout.status,
        "continue_url": continue_url,
        "stubbed_capabilities": snapshot.stubbed_capabilities,
        "capability_status": snapshot.capability_status,
        "blockers": snapshot.blockers,
        "warnings": warnings,
        "source_of_truth": snapshot.source_of_truth,
    }


async def get_checkout_session_view(checkout_id: str) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None:
        raise KeyError(checkout_id)
    events = await journal.list_events(checkout_id)
    checkout_json = checkout.model_dump() if hasattr(checkout, "model_dump") else checkout.dict()
    if checkout_json.get("continue_url"):
        checkout_json["continue_url"] = str(checkout_json["continue_url"]).format(checkout_id=checkout.checkout_id)
    return {
        "checkout": checkout_json,
        "events": [event.model_dump() if hasattr(event, "model_dump") else event.dict() for event in events],
    }


async def build_order_sync_audit(
    merchant_id: str,
    checkout_id: str,
    *,
    sample_limit: int = 10,
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)
    events = await journal.list_events(checkout_id)

    from db.database import database

    return await build_order_sync_audit_snapshot(
        merchant_id=merchant_id,
        checkout=checkout,
        readiness_events=events,
        get_order_fn=get_order,
        db=database,
        sample_limit=sample_limit,
    )


def _paid_like_for_return(status_value: Any) -> bool:
    status = str(status_value or "").strip().lower()
    return status in {"paid", "completed", "succeeded", "settled"}


def _fulfilled_like_for_return(status_value: Any) -> bool:
    status = str(status_value or "").strip().lower()
    return status in {
        "fulfilled",
        "shipped",
        "delivered",
        "partial",
        "partially_fulfilled",
        "partial_fulfilled",
        "success",
    }


def _normalize_return_probe_text(value: Any) -> str:
    return str(value or "").strip().lower()


async def _resolve_shopify_return_context(
    merchant_id: str,
    checkout_id: str,
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)

    payload = checkout.session_payload or {}
    if payload.get("merchant_alpha_mode") != "real_merchant_alpha":
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "message": "Return sync is only supported for the real-merchant alpha path.",
            }
        )

    order_id = str(checkout.order_id or "").strip()
    if not order_id:
        raise ValueError(
            {
                "code": "CHECKOUT_ORDER_NOT_CREATED",
                "checkout_id": checkout_id,
                "message": "Run /order-sync first so the checkout creates a local order before syncing returns.",
            }
        )

    order_row = await get_order(order_id)
    if not order_row:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Checkout references a local order that could not be loaded.",
            }
        )

    store_info = await get_primary_store(merchant_id)
    shopify_cfg = await _get_shopify_config_for_merchant(merchant_id)
    if not store_info or str(store_info.get("platform") or "").strip().lower() != "shopify":
        raise ValueError(
            {
                "code": "CHECKOUT_RETURN_SYNC_UNAVAILABLE",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Return sync requires a Shopify primary store for this merchant.",
            }
        )

    shop_domain = str(shopify_cfg.get("shop_domain") or store_info.get("domain") or "").strip()
    access_token = ""
    if shop_domain:
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
            store_id=str(store_info.get("store_id") or "").strip() or None,
        )
        access_token = str(access_token or "").strip()
    if not access_token:
        access_token = str(shopify_cfg.get("access_token") or "").strip()
    if not shop_domain or not access_token:
        raise ValueError(
            {
                "code": "CHECKOUT_RETURN_SYNC_UNAVAILABLE",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Return sync requires valid Shopify Admin credentials for this merchant.",
                "shop_domain_present": bool(shop_domain),
                "access_token_present": bool(access_token),
            }
        )

    return {
        "journal": journal,
        "checkout": checkout,
        "order_id": order_id,
        "order_row": order_row,
        "store_info": store_info,
        "shopify_cfg": shopify_cfg,
        "shop_domain": shop_domain,
        "access_token": access_token,
    }


async def sync_returns_for_checkout(
    merchant_id: str,
    checkout_id: str,
    *,
    api_version: Optional[str] = None,
    limit: int = 20,
    sample_limit: int = 10,
) -> Dict[str, Any]:
    context = await _resolve_shopify_return_context(merchant_id, checkout_id)
    checkout = context["checkout"]
    order_id = context["order_id"]
    order_row = context["order_row"]
    shopify_cfg = context["shopify_cfg"]
    shop_domain = context["shop_domain"]
    access_token = context["access_token"]

    from db.database import database

    return_sync_result = await sync_shopify_returns_best_effort(
        merchant_id=merchant_id,
        shop_domain=shop_domain,
        access_token=access_token,
        api_version=str(api_version or shopify_cfg.get("api_version") or "2025-10"),
        limit=limit,
        db=database,
    )
    audit = await build_order_sync_audit(
        merchant_id,
        checkout_id,
        sample_limit=sample_limit,
    )
    latest_order = await get_order(order_id) or order_row
    return {
        "checkout": checkout,
        "order": latest_order,
        "return_sync_result": return_sync_result,
        "audit": audit,
    }


def _build_return_eligibility_summary(
    *,
    checkout_id: str,
    order_id: str,
    order_row: Dict[str, Any],
    audit: Dict[str, Any],
    platform_probe: Dict[str, Any],
) -> Dict[str, Any]:
    order_state = audit.get("order_state") or {}
    local_status = _normalize_return_probe_text(order_row.get("status") or order_state.get("status"))
    local_payment_status = _normalize_return_probe_text(order_row.get("payment_status") or order_state.get("payment_status"))
    local_fulfillment_status = _normalize_return_probe_text(order_row.get("fulfillment_status") or order_state.get("fulfillment_status"))

    shopify_order = platform_probe.get("shopify_order") or {}
    order_probe = platform_probe.get("order_probe") or {}
    existing_returns = platform_probe.get("existing_returns") or []
    return_records = ((audit.get("evidence") or {}).get("return_records")) or []
    schema_diag = platform_probe.get("schema_diag") or {}
    return_capabilities = platform_probe.get("return_capabilities") or {}

    platform_payment_status = _normalize_return_probe_text(
        shopify_order.get("financial_status") or shopify_order.get("display_financial_status")
    )
    platform_fulfillment_status = _normalize_return_probe_text(
        shopify_order.get("fulfillment_status") or shopify_order.get("display_fulfillment_status")
    )
    platform_return_status = _normalize_return_probe_text(order_probe.get("returnStatus"))
    cancelled = bool(shopify_order.get("cancelled_at")) or local_status == "cancelled"
    shopify_order_id = str(order_row.get("shopify_order_id") or order_state.get("shopify_order_id") or "").strip()

    blockers: List[str] = []
    warnings: List[str] = []
    recommendations: List[str] = []

    if platform_probe.get("rest_error"):
        warnings.append("shopify_order_rest_probe_failed")
    if return_capabilities.get("queryroot_returnable_fulfillments_available") is False:
        warnings.append("shopify_queryroot_returnable_fulfillments_unavailable")
    if return_capabilities.get("order_returns_available") is False:
        warnings.append("shopify_order_returns_unavailable")
    if not shopify_order_id:
        blockers.append("merchant_writeback_not_observed")

    observed_return = bool(return_records) or bool(existing_returns) or platform_return_status in {"in_progress", "returned"}
    resolved_payment_status = local_payment_status or platform_payment_status
    resolved_fulfillment_status = local_fulfillment_status or platform_fulfillment_status

    if observed_return:
        status = "return_observed"
        recommendations.append("Return evidence already exists for this order. Run /return-sync after Shopify-side changes to refresh local return_records.")
    else:
        if cancelled:
            blockers.append("order_cancelled")
        if resolved_payment_status in {"refunded", "partially_refunded"}:
            blockers.append("order_already_refunded")
        elif not _paid_like_for_return(resolved_payment_status):
            blockers.append("order_not_paid")
        if not _fulfilled_like_for_return(resolved_fulfillment_status):
            blockers.append("order_not_fulfilled")

        status = "likely_eligible" if not blockers else "not_ready"
        if status == "likely_eligible":
            recommendations.append("Create the return in Shopify Admin for this order, then rerun /return-sync to pull it into return_records.")
        else:
            if "order_not_fulfilled" in blockers:
                recommendations.append("Use a fulfilled Shopify order for live return validation; unfulfilled orders are not good return canaries.")
            if "order_not_paid" in blockers or "order_already_refunded" in blockers:
                recommendations.append("Use a paid, non-refunded order for live return validation before expecting return_records to appear.")
            if "merchant_writeback_not_observed" in blockers:
                recommendations.append("Run /order-sync first so the readiness checkout is attached to a real Shopify order before probing returns.")
            if "order_cancelled" in blockers:
                recommendations.append("Choose a non-cancelled Shopify order for return validation.")

    return {
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "recommendations": recommendations,
        "resolved_payment_status": resolved_payment_status or None,
        "resolved_fulfillment_status": resolved_fulfillment_status or None,
        "platform_return_status": platform_return_status or None,
        "observed_return_record_count": len(return_records),
        "observed_shopify_return_count": len(existing_returns),
        "return_capabilities": {
            "queryroot_returnable_fulfillments_available": bool(
                return_capabilities.get("queryroot_returnable_fulfillments_available")
            ),
            "queryroot_returnable_fulfillment_available": bool(
                return_capabilities.get("queryroot_returnable_fulfillment_available")
            ),
            "order_return_status_available": bool(return_capabilities.get("order_return_status_available")),
            "order_returns_available": bool(return_capabilities.get("order_returns_available")),
        },
        "schema_diag": schema_diag,
        "local_order_state": {
            "status": local_status or None,
            "payment_status": local_payment_status or None,
            "fulfillment_status": local_fulfillment_status or None,
            "shopify_order_id": shopify_order_id or None,
        },
        "platform_order_state": {
            "financial_status": platform_payment_status or None,
            "fulfillment_status": platform_fulfillment_status or None,
            "cancelled_at": shopify_order.get("cancelled_at"),
            "closed_at": shopify_order.get("closed_at"),
        },
    }


async def probe_return_eligibility_for_checkout(
    merchant_id: str,
    checkout_id: str,
    *,
    api_version: Optional[str] = None,
    sample_limit: int = 10,
) -> Dict[str, Any]:
    context = await _resolve_shopify_return_context(merchant_id, checkout_id)
    checkout = context["checkout"]
    order_id = context["order_id"]
    order_row = context["order_row"]
    shopify_cfg = context["shopify_cfg"]
    shop_domain = context["shop_domain"]
    access_token = context["access_token"]

    platform_probe = await probe_shopify_return_eligibility_best_effort(
        shop_domain=shop_domain,
        access_token=access_token,
        api_version=str(api_version or shopify_cfg.get("api_version") or "2025-10"),
        shopify_order_id=str(order_row.get("shopify_order_id") or ""),
    )
    audit = await build_order_sync_audit(
        merchant_id,
        checkout_id,
        sample_limit=sample_limit,
    )
    eligibility = _build_return_eligibility_summary(
        checkout_id=checkout_id,
        order_id=order_id,
        order_row=order_row,
        audit=audit,
        platform_probe=platform_probe,
    )
    latest_order = await get_order(order_id) or order_row
    return {
        "checkout": checkout,
        "order": latest_order,
        "platform_probe": platform_probe,
        "eligibility": eligibility,
        "audit": audit,
    }


def _validate_checkout_buyer_context(checkout: CheckoutSessionRecord) -> Optional[str]:
    payload = checkout.session_payload or {}
    if not payload.get("buyer_email"):
        return "missing_buyer_email"
    shipping = payload.get("shipping_address") or {}
    required = ("name", "address_line1", "city", "postal_code", "country")
    if any(not shipping.get(field) for field in required):
        return "missing_shipping_address"
    return None


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _build_payment_action(*, psp_used: str, client_secret: Optional[str], redirect_url: Optional[str]) -> Dict[str, Any]:
    if redirect_url:
        return {"type": "redirect_url", "url": redirect_url}
    secret = str(client_secret or "").strip()
    psp = str(psp_used or "").strip().lower()
    if not secret:
        return {"type": None}
    if secret.startswith("http"):
        return {"type": "redirect_url", "url": secret}
    if psp == "stripe":
        return {"type": "stripe_client_secret", "client_secret": secret}
    if psp == "adyen":
        return {"type": "adyen_session", "client_secret": secret}
    return {"type": "client_secret", "client_secret": secret}


async def _create_local_order_for_checkout(checkout: CheckoutSessionRecord) -> str:
    payload = checkout.session_payload or {}
    quantity = int(checkout.quantity or 1)
    unit_price = float(((payload.get("price") or {}).get("amount")) or 0)
    currency = str(((payload.get("price") or {}).get("currency")) or "USD")
    order_data = {
        "merchant_id": checkout.merchant_id,
        "customer_name": payload.get("customer_name"),
        "customer_email": payload.get("buyer_email"),
        "shipping_address": payload.get("shipping_address") or {},
        "items": [
            {
                "product_id": payload.get("product_id"),
                "variant_id": payload.get("variant_id"),
                "product_title": payload.get("product_title"),
                "variant_title": payload.get("variant_title"),
                "sku": payload.get("sku"),
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": round(unit_price * quantity, 2),
            }
        ],
        "subtotal": round(unit_price * quantity, 2),
        "shipping_fee": 0.0,
        "tax": 0.0,
        "total": round(unit_price * quantity, 2),
        "currency": currency,
        "metadata": {
            "readiness_alpha": True,
            "channel": checkout.channel,
            "checkout_id": checkout.checkout_id,
            "merchant_alpha_mode": payload.get("merchant_alpha_mode"),
        },
        "store_id": ((payload.get("merchant_connection") or {}).get("store") or {}).get("store_id"),
        "psp_id": ((payload.get("payment_capabilities") or {}).get("psp_id")),
        "psp_used": ((payload.get("payment_capabilities") or {}).get("psp_provider")),
        "payment_method": None,
    }
    return await create_order(order_data)


async def attach_payment_reference_to_checkout(
    merchant_id: str,
    checkout_id: str,
    *,
    payment_reference: str,
    psp_used: Optional[str] = None,
    client_secret: Optional[str] = None,
    source: str = "external_payment_execution",
    mark_paid: bool = True,
    sync_shopify_transaction: bool = True,
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)

    payload = checkout.session_payload or {}
    if payload.get("merchant_alpha_mode") != "real_merchant_alpha":
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "message": "Payment bridge is only supported for the real-merchant alpha path.",
            }
        )

    order_id = str(checkout.order_id or "").strip()
    if not order_id:
        raise ValueError(
            {
                "code": "CHECKOUT_ORDER_NOT_CREATED",
                "checkout_id": checkout_id,
                "message": "Run /order-sync first so the checkout creates a local order before attaching payment state.",
            }
        )

    order_row = await get_order(order_id)
    if not order_row:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Checkout references a local order that could not be loaded.",
            }
        )

    payment_reference = str(payment_reference or "").strip()
    if not payment_reference:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "message": "payment_reference is required",
            }
        )

    order_payment_status = _normalized_payment_status(order_row)
    existing_payment_ref = str(order_row.get("payment_intent_id") or "").strip()
    replayed = False
    if order_payment_status == "paid":
        if existing_payment_ref and existing_payment_ref != payment_reference:
            raise ValueError(
                {
                    "code": "ORDER_ALREADY_PAID",
                    "checkout_id": checkout_id,
                    "order_id": order_id,
                    "message": "Order is already paid with a different payment reference.",
                    "existing_payment_reference": existing_payment_ref,
                }
            )
        replayed = True

    inferred_psp = _infer_psp_from_payment_reference(payment_reference)
    resolved_psp = (
        str(psp_used or "").strip().lower()
        or inferred_psp
        or str(order_row.get("psp_used") or "").strip().lower()
        or str(((payload.get("payment_capabilities") or {}).get("psp_provider")) or "").strip().lower()
    )
    if not resolved_psp:
        resolved_psp = "stripe"

    resolved_client_secret = (
        str(client_secret or "").strip()
        or str(order_row.get("client_secret") or "").strip()
        or payment_reference
    )

    if not replayed:
        await update_payment_info(
            order_id=order_id,
            payment_intent_id=payment_reference,
            client_secret=resolved_client_secret,
            payment_status="paid" if mark_paid else (order_payment_status or "processing"),
            psp_used=resolved_psp,
        )
        if mark_paid:
            await mark_order_paid(order_id)
        await log_order_event(
            event_type="readiness_payment_bridged",
            merchant_id=merchant_id,
            order_id=order_id,
            total_amount=_safe_float(order_row.get("total")),
            currency=str(order_row.get("currency") or "USD"),
            payment_method=resolved_psp,
            status="paid" if mark_paid else (order_payment_status or "processing"),
            metadata={
                "checkout_id": checkout_id,
                "payment_reference": payment_reference,
                "source": source,
                "mark_paid": mark_paid,
                "sync_shopify_transaction": sync_shopify_transaction,
            },
        )

    await journal.append_event(
        checkout_id=checkout_id,
        event_type="payment_reference_attached",
        event_payload={
            "order_id": order_id,
            "payment_reference": payment_reference,
            "psp_used": resolved_psp,
            "source": source,
            "mark_paid": mark_paid,
        },
    )

    transaction_sync: Dict[str, Any] = {"ok": False, "skipped": True, "reason": "shopify_sync_not_attempted"}
    next_payload = {
        "payment_reference": payment_reference,
        "payment_psp_used": resolved_psp,
        "payment_reference_type": (
            "stripe_checkout_session"
            if payment_reference.startswith("cs_")
            else ("payment_intent" if payment_reference.startswith("pi_") else payload.get("payment_reference_type"))
        ),
        "checkout_session_id": payment_reference if payment_reference.startswith("cs_") else payload.get("checkout_session_id"),
        "payment_bridge": {
            "source": source,
            "mark_paid": mark_paid,
            "sync_shopify_transaction": sync_shopify_transaction,
        },
    }

    refreshed_order = await get_order(order_id)
    shopify_order_id = str((refreshed_order or {}).get("shopify_order_id") or "").strip()
    merchant_connection = payload.get("merchant_connection") or {}
    shopify_conn = merchant_connection.get("shopify") or {}
    shop_domain = str(shopify_conn.get("shop_domain") or "").strip()
    access_token = str(shopify_conn.get("access_token") or "").strip()
    amount = _safe_float((payload.get("price") or {}).get("amount")) * int(checkout.quantity or 1)
    currency = str(((payload.get("price") or {}).get("currency")) or (refreshed_order or {}).get("currency") or "USD")

    if sync_shopify_transaction and shopify_order_id and shop_domain and access_token:
        try:
            transaction_sync = await ensure_external_payment_transaction_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                shopify_order_id=shopify_order_id,
                psp_used=resolved_psp,
                external_payment_ref=payment_reference,
                amount=amount,
                currency=currency,
                pivota_order_id=order_id,
            )
        except Exception:
            logger.warning("Payment transaction sync failed for checkout=%s", checkout_id, exc_info=True)
            transaction_sync = {"ok": False, "skipped": False, "reason": "transaction_sync_failed"}
        parent_transaction_id = _coerce_int(transaction_sync.get("parent_transaction_id"))
        if parent_transaction_id is not None:
            refreshed_for_metadata = await get_order(order_id) or refreshed_order or order_row
            merged_metadata = _normalized_metadata(refreshed_for_metadata)
            merged_metadata["shopify_parent_transaction_id"] = parent_transaction_id
            if transaction_sync.get("parent_transaction_gateway"):
                merged_metadata["shopify_parent_transaction_gateway"] = transaction_sync.get("parent_transaction_gateway")
            if transaction_sync.get("parent_transaction_source"):
                merged_metadata["shopify_parent_transaction_source"] = transaction_sync.get("parent_transaction_source")
            await update_order(order_id, {"metadata": merged_metadata})
            next_payload["shopify_parent_transaction_id"] = parent_transaction_id
            if transaction_sync.get("parent_transaction_gateway"):
                next_payload["shopify_parent_transaction_gateway"] = transaction_sync.get("parent_transaction_gateway")
        if transaction_sync.get("ok"):
            await journal.append_event(
                checkout_id=checkout_id,
                event_type="merchant_payment_transaction_synced",
                event_payload={
                    "order_id": order_id,
                    "shopify_order_id": shopify_order_id,
                    "payment_reference": payment_reference,
                    "psp_used": resolved_psp,
                    "created": transaction_sync.get("created"),
                    "parent_transaction_id": parent_transaction_id,
                    "parent_transaction_gateway": transaction_sync.get("parent_transaction_gateway"),
                },
            )
    elif sync_shopify_transaction:
        transaction_sync = {"ok": False, "skipped": True, "reason": "shopify_order_or_credentials_missing"}

    updated_checkout = await journal.update_checkout_session(
        checkout_id,
        session_payload_patch=next_payload,
    )
    return {
        "checkout": updated_checkout or checkout,
        "events": await journal.list_events(checkout_id),
        "order": await get_order(order_id),
        "payment_reference": payment_reference,
        "psp_used": resolved_psp,
        "transaction_sync": transaction_sync,
        "replayed": replayed,
    }


async def create_payment_intent_for_checkout(
    merchant_id: str,
    checkout_id: str,
    *,
    preferred_psps: Optional[List[str]] = None,
    psp_mode: Optional[str] = None,
    test_psp_probe: bool = False,
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)

    payload = checkout.session_payload or {}
    if payload.get("merchant_alpha_mode") != "real_merchant_alpha":
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "message": "Payment intent creation is only supported for the real-merchant alpha path.",
            }
        )

    order_id = str(checkout.order_id or "").strip()
    if not order_id:
        raise ValueError(
            {
                "code": "CHECKOUT_ORDER_NOT_CREATED",
                "checkout_id": checkout_id,
                "message": "Run /order-sync first so the checkout creates a local order before creating a payment intent.",
            }
        )

    order_row = await get_order(order_id)
    if not order_row:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Checkout references a local order that could not be loaded.",
            }
        )

    existing_payment_intent_id = str(order_row.get("payment_intent_id") or "").strip()
    existing_payment_status = _normalized_payment_status(order_row)
    existing_client_secret = str(order_row.get("client_secret") or "").strip()
    existing_psp_used = str(order_row.get("psp_used") or "").strip().lower()
    if existing_payment_intent_id:
        return {
            "checkout": checkout,
            "events": await journal.list_events(checkout_id),
            "order": order_row,
            "payment_intent_id": existing_payment_intent_id,
            "client_secret": existing_client_secret or None,
            "psp_used": existing_psp_used or None,
            "payment_intent_status": existing_payment_status or "processing",
            "payment_action": _build_payment_action(
                psp_used=existing_psp_used,
                client_secret=existing_client_secret,
                redirect_url=None,
            ),
            "replayed": True,
            "bridged_to_paid": existing_payment_status == "paid",
        }

    amount = Decimal(str(order_row.get("total") or _safe_float((payload.get("price") or {}).get("amount")) * int(checkout.quantity or 1)))
    currency = str(order_row.get("currency") or ((payload.get("price") or {}).get("currency")) or "USD")
    metadata = {
        "order_id": order_id,
        "merchant_id": merchant_id,
        "checkout_id": checkout_id,
        "description": payload.get("product_title") or order_id,
    }
    if psp_mode:
        metadata["psp_mode"] = str(psp_mode).strip()

    enforce_live_readiness = _resolve_checkout_live_readiness_requirement(
        merchant_id=merchant_id,
        psp_mode=psp_mode,
        test_psp_probe=test_psp_probe,
    )

    success, payment_intent, error, psp_used = await create_payment_with_failover(
        merchant_id=merchant_id,
        amount=amount,
        currency=currency,
        metadata=metadata,
        preferred_psps=preferred_psps,
        canonical_psp_required=True,
        enforce_live_readiness=enforce_live_readiness,
    )
    if not success or payment_intent is None:
        raise ValueError(
            {
                "code": "PAYMENT_FAILED",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": str(error or "Payment intent creation failed."),
                "psp_used": psp_used,
            }
        )

    payment_status = "awaiting_payment"
    if str(payment_intent.status or "").lower() == "succeeded":
        payment_status = "paid"
    elif str(payment_intent.status or "").lower() in {"processing"}:
        payment_status = "processing"

    await update_payment_info(
        order_id=order_id,
        payment_intent_id=payment_intent.id,
        client_secret=getattr(payment_intent, "client_secret", None) or "",
        payment_status=payment_status,
        psp_used=psp_used,
    )
    await log_order_event(
        event_type="readiness_payment_intent_created",
        merchant_id=merchant_id,
        order_id=order_id,
        total_amount=float(amount),
        currency=currency,
        payment_method=psp_used,
        status=payment_status,
        metadata={
            "checkout_id": checkout_id,
            "payment_intent_id": payment_intent.id,
            "payment_intent_status": payment_intent.status,
            "psp_used": psp_used,
            "psp_mode": psp_mode,
            "preferred_psps": preferred_psps or [],
        },
    )
    await journal.append_event(
        checkout_id=checkout_id,
        event_type="payment_intent_created",
        event_payload={
            "order_id": order_id,
            "payment_intent_id": payment_intent.id,
            "payment_intent_status": payment_intent.status,
            "psp_used": psp_used,
        },
    )
    checkout = await journal.update_checkout_session(
        checkout_id,
        session_payload_patch={
            "payment_intent_id": payment_intent.id,
            "payment_psp_used": psp_used,
            "payment_intent_status": payment_intent.status,
            "payment_reference_type": "stripe_checkout_session" if str(payment_intent.id or "").startswith("cs_") else "payment_intent",
            "checkout_session_id": payment_intent.id if str(payment_intent.id or "").startswith("cs_") else None,
        },
    ) or checkout

    bridged_to_paid = False
    if str(payment_intent.status or "").lower() == "succeeded":
        bridged = await attach_payment_reference_to_checkout(
            merchant_id,
            checkout_id,
            payment_reference=payment_intent.id,
            psp_used=psp_used,
            client_secret=getattr(payment_intent, "client_secret", None),
            source="readiness_payment_intent_succeeded",
            mark_paid=True,
            sync_shopify_transaction=True,
        )
        checkout = bridged["checkout"]
        order_row = bridged["order"]
        bridged_to_paid = True
    else:
        order_row = await get_order(order_id)

    return {
        "checkout": checkout,
        "events": await journal.list_events(checkout_id),
        "order": order_row,
        "payment_intent_id": payment_intent.id,
        "client_secret": getattr(payment_intent, "client_secret", None),
        "psp_used": psp_used,
        "payment_intent_status": payment_intent.status,
        "payment_action": _build_payment_action(
            psp_used=psp_used,
            client_secret=getattr(payment_intent, "client_secret", None),
            redirect_url=getattr(payment_intent, "redirect_url", None),
        ),
        "replayed": False,
        "bridged_to_paid": bridged_to_paid,
    }


async def sync_payment_status_for_checkout(
    merchant_id: str,
    checkout_id: str,
    *,
    mark_paid_on_success: bool = True,
    sync_shopify_transaction: bool = True,
    source: str = "readiness_payment_status_sync",
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)

    payload = checkout.session_payload or {}
    if payload.get("merchant_alpha_mode") != "real_merchant_alpha":
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "message": "Payment status sync is only supported for the real-merchant alpha path.",
            }
        )

    order_id = str(checkout.order_id or "").strip()
    if not order_id:
        raise ValueError(
            {
                "code": "CHECKOUT_ORDER_NOT_CREATED",
                "checkout_id": checkout_id,
                "message": "Run /order-sync first so the checkout creates a local order before syncing payment status.",
            }
        )

    order_row = await get_order(order_id)
    if not order_row:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Checkout references a local order that could not be loaded.",
            }
        )

    payment_reference = str(order_row.get("payment_intent_id") or "").strip()
    if not payment_reference:
        raise ValueError(
            {
                "code": "CHECKOUT_PAYMENT_INTENT_NOT_FOUND",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Create or attach a payment intent before syncing payment status.",
            }
        )

    external_status = await _query_payment_intent_status(
        merchant_id,
        payment_reference=payment_reference,
        psp_used=str(order_row.get("psp_used") or (payload.get("payment_psp_used") or "")).strip().lower() or None,
    )
    if not external_status.get("ok"):
        raise ValueError(
            {
                "code": "PAYMENT_STATUS_SYNC_FAILED",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "payment_intent_id": payment_reference,
                "psp_used": external_status.get("psp_used"),
                "message": str(external_status.get("error") or "PSP payment status lookup failed."),
            }
        )

    normalized_status = str(external_status.get("normalized_status") or "unknown")
    current_payment_status = _normalized_payment_status(order_row)
    resolved_psp = str(external_status.get("psp_used") or order_row.get("psp_used") or "").strip().lower() or None
    existing_client_secret = str(order_row.get("client_secret") or "").strip()
    resolved_payment_reference = str(external_status.get("resolved_payment_reference") or payment_reference).strip()
    resolved_client_secret = str(external_status.get("resolved_client_secret") or existing_client_secret).strip()
    payment_reference_type = str(
        external_status.get("payment_reference_type")
        or payload.get("payment_reference_type")
        or ("stripe_checkout_session" if payment_reference.startswith("cs_") else "payment_intent")
    ).strip()
    checkout_session_id = str(
        external_status.get("checkout_session_id")
        or payload.get("checkout_session_id")
        or (payment_reference if payment_reference.startswith("cs_") else "")
    ).strip() or None

    bridged_to_paid = False
    transaction_sync: Dict[str, Any] = {"ok": False, "skipped": True, "reason": "payment_not_paid"}
    replayed = current_payment_status == normalized_status

    if normalized_status == "paid" and mark_paid_on_success:
        bridged = await attach_payment_reference_to_checkout(
            merchant_id,
            checkout_id,
            payment_reference=resolved_payment_reference,
            psp_used=resolved_psp,
            client_secret=resolved_client_secret,
            source=source,
            mark_paid=True,
            sync_shopify_transaction=sync_shopify_transaction,
        )
        checkout = bridged["checkout"]
        order_row = bridged["order"] or order_row
        transaction_sync = bridged.get("transaction_sync") or transaction_sync
        replayed = bool(bridged.get("replayed"))
        bridged_to_paid = True
    else:
        if normalized_status in {"awaiting_payment", "processing", "failed", "cancelled"} and (
            normalized_status != current_payment_status or resolved_payment_reference != payment_reference
        ):
            await update_payment_info(
                order_id=order_id,
                payment_intent_id=resolved_payment_reference,
                client_secret=resolved_client_secret or resolved_payment_reference,
                payment_status=normalized_status,
                psp_used=resolved_psp,
            )
        await log_order_event(
            event_type="readiness_payment_status_synced",
            merchant_id=merchant_id,
            order_id=order_id,
            total_amount=_safe_float(order_row.get("total")),
            currency=str(order_row.get("currency") or "USD"),
            payment_method=resolved_psp,
            status=normalized_status,
            metadata={
                "checkout_id": checkout_id,
                "payment_intent_id": resolved_payment_reference,
                "payment_reference": payment_reference,
                "payment_reference_type": payment_reference_type,
                "checkout_session_id": checkout_session_id,
                "raw_payment_status": external_status.get("raw_status"),
                "normalized_payment_status": normalized_status,
                "source": source,
            },
        )
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="payment_status_synced",
            event_payload={
                "order_id": order_id,
                "payment_intent_id": resolved_payment_reference,
                "payment_reference": payment_reference,
                "payment_reference_type": payment_reference_type,
                "checkout_session_id": checkout_session_id,
                "raw_payment_status": external_status.get("raw_status"),
                "normalized_payment_status": normalized_status,
                "psp_used": resolved_psp,
                "source": source,
            },
        )
        checkout = await journal.update_checkout_session(
            checkout_id,
            session_payload_patch={
                "payment_intent_id": resolved_payment_reference,
                "payment_psp_used": resolved_psp,
                "payment_intent_status": external_status.get("raw_status"),
                "payment_reference": payment_reference,
                "payment_reference_type": payment_reference_type,
                "checkout_session_id": checkout_session_id,
                "payment_status_synced_at": _utc_now_iso(),
            },
        ) or checkout
        order_row = await get_order(order_id) or order_row

    return {
        "checkout": checkout,
        "events": await journal.list_events(checkout_id),
        "order": order_row,
        "payment_intent_id": resolved_payment_reference,
        "payment_reference": payment_reference,
        "payment_reference_type": payment_reference_type,
        "checkout_session_id": checkout_session_id,
        "payment_intent_status": external_status.get("raw_status"),
        "normalized_payment_status": normalized_status,
        "psp_used": resolved_psp,
        "transaction_sync": transaction_sync,
        "replayed": replayed,
        "bridged_to_paid": bridged_to_paid,
    }


async def create_refund_for_checkout(
    merchant_id: str,
    checkout_id: str,
    *,
    amount: Optional[float] = None,
    reason: str = "readiness_alpha_refund",
    source: str = "readiness_alpha_refund",
    idempotency_key: Optional[str] = None,
    sync_shopify_refund_transaction: bool = True,
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)

    payload = checkout.session_payload or {}
    if payload.get("merchant_alpha_mode") != "real_merchant_alpha":
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "message": "Refund is only supported for the real-merchant alpha path.",
            }
        )

    order_id = str(checkout.order_id or "").strip()
    if not order_id:
        raise ValueError(
            {
                "code": "CHECKOUT_ORDER_NOT_CREATED",
                "checkout_id": checkout_id,
                "message": "Run /order-sync first so the checkout creates a local order before refunding it.",
            }
        )

    order_row = await get_order(order_id)
    if not order_row:
        raise ValueError(
            {
                "code": "CHECKOUT_INVALID",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "message": "Checkout references a local order that could not be loaded.",
            }
        )

    payment_status = _normalized_payment_status(order_row)
    if payment_status not in {"paid", "completed", "partially_refunded"}:
        raise ValueError(
            {
                "code": "CHECKOUT_REFUND_NOT_ELIGIBLE",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "payment_status": payment_status or None,
                "message": "Refund requires a paid or partially_refunded order.",
            }
        )

    order_total = Decimal(str(order_row.get("total") or "0"))
    total_refunded = Decimal(str(order_row.get("total_refunded") or "0"))
    remaining = max(order_total - total_refunded, Decimal("0"))
    if remaining <= Decimal("0"):
        raise ValueError(
            {
                "code": "CHECKOUT_REFUND_NOT_ELIGIBLE",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "payment_status": payment_status or None,
                "message": "Order is already fully refunded.",
            }
        )

    refund_amount = Decimal(str(amount)) if amount is not None else remaining
    if refund_amount <= Decimal("0") or refund_amount > remaining:
        raise ValueError(
            {
                "code": "CHECKOUT_REFUND_NOT_ELIGIBLE",
                "checkout_id": checkout_id,
                "order_id": order_id,
                "payment_status": payment_status or None,
                "remaining_refundable_amount": float(remaining),
                "message": f"Refund amount must be > 0 and <= remaining refundable amount {float(remaining):.2f}.",
            }
        )

    refund_result = await refund_service.create_refund(
        order_id=order_id,
        amount=float(refund_amount),
        reason=str(reason or "readiness_alpha_refund").strip() or "readiness_alpha_refund",
        source=str(source or "readiness_alpha_refund").strip() or "readiness_alpha_refund",
        created_by="readiness_internal",
        idempotency_key=idempotency_key,
    )

    refund_status = str((refund_result or {}).get("status") or "unknown")
    refund_id = (refund_result or {}).get("refund_id")
    psp_refund_id = (refund_result or {}).get("psp_refund_id")
    platform_refund_id = (refund_result or {}).get("platform_refund_id") or psp_refund_id

    await log_order_event(
        event_type="readiness_refund_requested",
        merchant_id=merchant_id,
        order_id=order_id,
        total_amount=float(refund_amount),
        currency=str(order_row.get("currency") or "USD"),
        payment_method=str(order_row.get("psp_used") or "").strip().lower() or None,
        status=refund_status,
        metadata={
            "checkout_id": checkout_id,
            "refund_id": refund_id,
            "psp_refund_id": psp_refund_id,
            "platform_refund_id": platform_refund_id,
            "amount": float(refund_amount),
            "reason": reason,
            "source": source,
            "idempotency_key": idempotency_key,
        },
    )

    await journal.append_event(
        checkout_id=checkout_id,
        event_type="refund_requested" if refund_status != "failed" else "refund_processing_enqueued",
        event_payload={
            "order_id": order_id,
            "refund_id": refund_id,
            "psp_refund_id": psp_refund_id,
            "platform_refund_id": platform_refund_id,
            "amount": float(refund_amount),
            "reason": reason,
            "refund_status": refund_status,
        },
    )

    refreshed_order = await get_order(order_id) or order_row
    transaction_sync: Dict[str, Any] = {"ok": False, "skipped": True, "reason": "refund_not_completed"}

    if refund_status in {"success", "duplicate"} and sync_shopify_refund_transaction:
        shopify_order_id = str((refreshed_order or {}).get("shopify_order_id") or "").strip()
        order_metadata = _normalized_metadata(refreshed_order)
        known_parent_transaction_id = _coerce_int(order_metadata.get("shopify_parent_transaction_id"))
        merchant_connection = payload.get("merchant_connection") or {}
        shopify_conn = merchant_connection.get("shopify") or {}
        shop_domain = str(shopify_conn.get("shop_domain") or "").strip()
        access_token = str(shopify_conn.get("access_token") or "").strip()
        currency = str((refreshed_order or {}).get("currency") or ((payload.get("price") or {}).get("currency")) or "USD")
        if shopify_order_id and shop_domain and access_token:
            try:
                transaction_sync = await ensure_external_refund_transaction_best_effort(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    shopify_order_id=shopify_order_id,
                    psp_used=str((refreshed_order or {}).get("psp_used") or payload.get("payment_psp_used") or "").strip().lower() or None,
                    external_refund_ref=str(psp_refund_id or refund_id or "").strip() or None,
                    amount=float(refund_amount),
                    currency=currency,
                    parent_transaction_id=known_parent_transaction_id,
                    pivota_order_id=order_id,
                )
            except Exception:
                logger.warning("Refund transaction sync failed for checkout=%s", checkout_id, exc_info=True)
                transaction_sync = {"ok": False, "skipped": False, "reason": "refund_transaction_sync_failed"}

    await log_order_event(
        event_type="readiness_refund_transaction_sync",
        merchant_id=merchant_id,
        order_id=order_id,
        total_amount=float(refund_amount),
        currency=str((refreshed_order or order_row).get("currency") or "USD"),
        payment_method=str((refreshed_order or order_row).get("psp_used") or "").strip().lower() or None,
        status=(
            "soft_skipped"
            if transaction_sync.get("soft_skipped")
            else ("ready" if transaction_sync.get("ok") else "failed")
        ),
        metadata={
            "checkout_id": checkout_id,
            "refund_id": refund_id,
            "psp_refund_id": psp_refund_id,
            "platform_refund_id": platform_refund_id,
            "transaction_sync": transaction_sync,
            "source": source,
        },
    )

    reconciled = await _reconcile_checkout_state_from_order(
        journal=journal,
        checkout=checkout,
        order_row=refreshed_order,
    )
    if reconciled is not None:
        checkout = reconciled
    else:
        checkout = await journal.update_checkout_session(
            checkout_id,
            session_payload_patch={
                "last_refund": {
                    "refund_id": refund_id,
                    "psp_refund_id": psp_refund_id,
                    "platform_refund_id": platform_refund_id,
                    "amount": float(refund_amount),
                    "refund_status": refund_status,
                    "source": source,
                }
            },
        ) or checkout

    return {
        "checkout": checkout,
        "events": await journal.list_events(checkout_id),
        "order": refreshed_order,
        "refund_status": refund_status,
        "refund_id": refund_id,
        "psp_refund_id": psp_refund_id,
        "platform_refund_id": platform_refund_id,
        "amount": float(refund_amount),
        "remaining_refundable_before": float(remaining),
        "transaction_sync": transaction_sync,
        "replayed": refund_status == "duplicate",
    }


def _to_shopify_shipping_address(shipping_address: Dict[str, Any]) -> Dict[str, Any]:
    name = str(shipping_address.get("name") or "Customer").strip()
    first_name, _, last_name = name.partition(" ")
    return {
        "first_name": first_name or "Customer",
        "last_name": last_name or "",
        "address1": shipping_address.get("address_line1"),
        "address2": shipping_address.get("address_line2"),
        "city": shipping_address.get("city"),
        "province": shipping_address.get("state"),
        "zip": shipping_address.get("postal_code"),
        "country": shipping_address.get("country"),
        "phone": shipping_address.get("phone"),
    }


async def _create_shopify_order_for_checkout(
    *,
    checkout: CheckoutSessionRecord,
    shop_domain: str,
    access_token: str,
) -> Dict[str, Any]:
    payload = checkout.session_payload or {}
    variant_id = payload.get("variant_id")
    if not variant_id:
        return {"ok": False, "code": "missing_variant_id"}
    line_item: Dict[str, Any]
    if str(variant_id).isdigit():
        line_item = {"variant_id": int(str(variant_id)), "quantity": checkout.quantity}
    else:
        line_item = {
            "title": payload.get("product_title") or "Product",
            "quantity": checkout.quantity,
            "price": str(((payload.get("price") or {}).get("amount")) or "0"),
            "taxable": False,
        }

    order_payload = {
        "order": {
            "email": payload.get("buyer_email"),
            "financial_status": "pending",
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            "line_items": [line_item],
            "shipping_address": _to_shopify_shipping_address(payload.get("shipping_address") or {}),
            "note": f"Pivota readiness alpha checkout_id={checkout.checkout_id}",
            "tags": "pivota,readiness-alpha",
        }
    }
    url = f"https://{shop_domain}/admin/api/2025-10/orders.json"
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(
            url,
            json=order_payload,
            headers={"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"},
        )
    if response.status_code != 201:
        return {
            "ok": False,
            "code": "merchant_writeback_failed",
            "status_code": response.status_code,
            "error": response.text[:500],
        }
    order = (response.json() or {}).get("order") or {}
    return {
        "ok": True,
        "shopify_order_id": str(order.get("id")),
        "shopify_order_name": order.get("name"),
        "shopify_order_url": f"https://{shop_domain}/admin/orders/{order.get('id')}" if order.get("id") else None,
    }


def _extract_linked_merchant_order(order_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not order_row:
        return None

    metadata = order_row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    merchant_order = metadata.get("merchant_order")
    if isinstance(merchant_order, dict):
        platform_order_id = str(merchant_order.get("platform_order_id") or "").strip()
        if platform_order_id:
            payload = dict(merchant_order)
            payload["platform_order_id"] = platform_order_id
            return payload

    shopify_order_id = str(order_row.get("shopify_order_id") or "").strip()
    if shopify_order_id:
        return {
            "platform": "shopify",
            "platform_order_id": shopify_order_id,
        }
    return None


async def _write_back_order_for_checkout(
    *,
    merchant_id: str,
    checkout: CheckoutSessionRecord,
    order_id: str,
    order_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    linked_order = _extract_linked_merchant_order(order_row)
    if linked_order:
        return {"ok": True, "linked_order": linked_order}

    payload = checkout.session_payload or {}
    merchant_connection = payload.get("merchant_connection") or {}
    store_payload = merchant_connection.get("store") or {}
    platform = str(store_payload.get("platform") or "").strip().lower()

    if platform in {"", "shopify"}:
        shopify_conn = merchant_connection.get("shopify") or {}
        store_info = None
        if not shopify_conn or not shopify_conn.get("shop_domain"):
            try:
                store_info = await get_primary_store(merchant_id)
            except Exception:
                store_info = None
        shop_domain = str(
            shopify_conn.get("shop_domain")
            or ((store_info or {}).get("domain"))
            or ""
        ).strip()
        access_token = str(shopify_conn.get("access_token") or "").strip()
        if not access_token and shop_domain:
            try:
                access_token, _ = await resolve_shopify_admin_access_token(
                    shop_domain=shop_domain,
                    api_key_raw=((store_info or {}).get("api_key_raw") or (store_info or {}).get("api_key")),
                    store_id=str((store_info or {}).get("store_id") or "").strip() or None,
                )
                access_token = str(access_token or "").strip()
            except Exception:
                access_token = ""
        if not shop_domain or not access_token:
            try:
                shopify_cfg = await _get_shopify_config_for_merchant(merchant_id)
            except Exception:
                shopify_cfg = {}
            shop_domain = str(shop_domain or shopify_cfg.get("shop_domain") or "").strip()
            access_token = str(access_token or shopify_cfg.get("access_token") or "").strip()
        if not shop_domain or not access_token:
            return {
                "ok": False,
                "code": "shopify_configuration_missing",
                "platform": "shopify",
            }

        writeback = await _create_shopify_order_for_checkout(
            checkout=checkout,
            shop_domain=shop_domain,
            access_token=access_token,
        )
        if not writeback.get("ok"):
            return {
                **writeback,
                "platform": "shopify",
            }

        await update_fulfillment_info(
            order_id=order_id,
            shopify_order_id=writeback.get("shopify_order_id"),
            fulfillment_status="processing",
        )
        return {
            "ok": True,
            "linked_order": {
                "platform": "shopify",
                "platform_order_id": str(writeback.get("shopify_order_id") or "").strip(),
                "platform_order_name": writeback.get("shopify_order_name"),
                "platform_order_url": writeback.get("shopify_order_url"),
                "store_id": str(store_payload.get("store_id") or "").strip() or None,
                "domain": shop_domain,
            },
        }

    from routes.order_routes import sync_order_to_connected_store

    writeback_ok = await sync_order_to_connected_store(order_id)
    refreshed_order = await get_order(order_id)
    linked_order = _extract_linked_merchant_order(refreshed_order)
    if not writeback_ok or not linked_order:
        return {
            "ok": False,
            "code": "merchant_order_create_failed",
            "platform": platform or None,
        }
    return {"ok": True, "linked_order": linked_order}


async def advance_order_sync(
    merchant_id: str,
    checkout_id: str,
    *,
    replay: bool = False,
) -> Dict[str, Any]:
    journal = get_default_journal()
    checkout = await journal.get_checkout_session(checkout_id)
    if checkout is None or checkout.merchant_id != merchant_id:
        raise KeyError(checkout_id)

    payload = checkout.session_payload or {}
    if payload.get("merchant_alpha_mode") != "real_merchant_alpha":
        return await journal.advance_order_sync(checkout_id)

    snapshot = await build_readiness_snapshot(merchant_id, channel=checkout.channel or "ucp")
    events_before = await journal.list_events(checkout_id)
    event_types = {event.event_type for event in events_before}

    if snapshot.capability_status.get("checkout") != "ready":
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="checkout_blocked",
            event_payload={"blockers": snapshot.blockers or ["merchant_checkout_capability_missing"]},
        )
        updated = await journal.update_checkout_session(checkout_id, status="blocked")
        return {
            "checkout": updated,
            "events": await journal.list_events(checkout_id),
            "replayed": "checkout_blocked" in event_types,
        }

    buyer_context_error = _validate_checkout_buyer_context(checkout)
    if buyer_context_error:
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="checkout_blocked",
            event_payload={"blockers": [buyer_context_error]},
        )
        updated = await journal.update_checkout_session(checkout_id, status="blocked")
        return {
            "checkout": updated,
            "events": await journal.list_events(checkout_id),
            "replayed": "checkout_blocked" in event_types,
        }

    if "payment_capability_verified" not in event_types:
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="payment_capability_verified",
            event_payload={
                "psp_provider": snapshot.source_of_truth.get("checkout_capability"),
                "merchant_alpha_mode": snapshot.merchant_alpha_mode,
            },
        )

    if not checkout.order_id:
        order_id = await _create_local_order_for_checkout(checkout)
        checkout = await journal.update_checkout_session(checkout_id, status="created", order_id=order_id)
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="order_created",
            event_payload={"order_id": order_id, "mode": "local_orders_table"},
        )
    else:
        order_id = checkout.order_id

    order_row = await get_order(order_id)
    linked_order = _extract_linked_merchant_order(order_row)
    if order_row and linked_order:
        reconciled = await _reconcile_checkout_state_from_order(
            journal=journal,
            checkout=checkout,
            order_row=order_row,
        )
        if reconciled is not None:
            return {
                "checkout": reconciled,
                "events": await journal.list_events(checkout_id),
                "replayed": replay,
            }
        if "order_forwarded_to_merchant" not in event_types:
            await journal.append_event(
                checkout_id=checkout_id,
                event_type="order_forwarded_to_merchant",
                event_payload=linked_order,
            )
        if "state_synced" not in event_types:
            await journal.append_event(
                checkout_id=checkout_id,
                event_type="state_synced",
                event_payload={"status": "state_synced", "order_id": order_id},
            )
        updated = await journal.update_checkout_session(checkout_id, status="state_synced")
        return {"checkout": updated, "events": await journal.list_events(checkout_id), "replayed": replay}

    writeback = await _write_back_order_for_checkout(
        merchant_id=merchant_id,
        checkout=checkout,
        order_id=order_id,
        order_row=order_row,
    )
    linked_order = writeback.get("linked_order")
    if not writeback.get("ok") or not linked_order:
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="merchant_writeback_failed",
            event_payload={
                **writeback,
                "platform": str(writeback.get("platform") or ((((payload.get("merchant_connection") or {}).get("store") or {}).get("platform")) or "")).strip() or None,
            },
        )
        updated = await journal.update_checkout_session(checkout_id, status="failed")
        return {"checkout": updated, "events": await journal.list_events(checkout_id), "replayed": "merchant_writeback_failed" in event_types}
    await journal.append_event(
        checkout_id=checkout_id,
        event_type="order_forwarded_to_merchant",
        event_payload=linked_order,
    )
    await journal.update_checkout_session(
        checkout_id,
        status="forwarded",
        session_payload_patch={"merchant_order": linked_order},
    )

    await journal.append_event(
        checkout_id=checkout_id,
        event_type="state_synced",
        event_payload={"status": "state_synced", "order_id": order_id},
    )
    updated = await journal.update_checkout_session(checkout_id, status="state_synced")
    return {
        "checkout": updated,
        "events": await journal.list_events(checkout_id),
        "replayed": replay and "state_synced" in event_types,
    }
