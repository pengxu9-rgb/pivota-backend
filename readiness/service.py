from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
import stripe

from adapters.multi_psp_orchestrator import MultiPSPOrchestrator, create_payment_with_failover
from adapters.psp_adapter import get_psp_adapter
from db.orders import create_order, get_order, mark_order_paid, update_fulfillment_info, update_order, update_payment_info
from db.products import log_order_event
from readiness.channel_exports.ucp import build_ucp_export
from readiness.flags import readiness_alpha_merchant_id
from readiness.models import ChannelReadinessReport, CheckoutSessionRecord, MerchantReadinessSnapshot, ReadyProduct, ReadyVariant
from readiness.order_sync import get_default_journal
from readiness.scoring import build_merchant_snapshot, find_ready_variant
from readiness.sources import load_merchant_source_dataset, supported_merchant_ids
from readiness.sync_audit import build_order_sync_audit_snapshot
from services.refund_service import refund_service
from services.shopify_transactions_service import (
    ensure_external_payment_transaction_best_effort,
    ensure_external_refund_transaction_best_effort,
)

logger = logging.getLogger(__name__)


class UnsupportedMerchantError(KeyError):
    pass


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


def build_export_summary_response(
    snapshot: MerchantReadinessSnapshot,
    *,
    sample_limit: int = 25,
) -> Dict[str, Any]:
    offer_ids_sample: List[str] = []
    product_ids_sample: List[str] = []
    availability_counts: Counter[str] = Counter()
    currency_counts: Counter[str] = Counter()
    review_backed_offer_count = 0
    offer_count = 0

    for product in snapshot.products:
        for variant in product.variants:
            if variant.channel_coverage.get("ucp") != "ready":
                continue
            offer_count += 1
            _append_sample(product_ids_sample, product.product_id, sample_limit=sample_limit)
            _append_sample(
                offer_ids_sample,
                f"ucp:{snapshot.merchant_id}:{product.product_id}:{variant.variant_id}",
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
            if coverage.channel == "ucp"
        ),
        0,
    )
    validation_warnings = list(snapshot.warnings)
    if snapshot.capability_status.get("reviews_confidence") == "blocked":
        validation_warnings.append("review summaries are unavailable for the readiness model")
    elif review_backed_offer_count < offer_count:
        validation_warnings.append("review coverage is partial across exported offers")
    if snapshot.merchant_alpha_mode != "real_merchant_alpha":
        validation_warnings.append("checkout execution is stubbed for this thin slice")
        validation_warnings.append("merchant write-back is stubbed for this thin slice")

    return {
        "export_version": "readiness_ucp_export.v1",
        "merchant_id": snapshot.merchant_id,
        "channel": "ucp",
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


async def build_readiness_snapshot(merchant_id: str, channel: str = "ucp") -> MerchantReadinessSnapshot:
    try:
        dataset = await load_merchant_source_dataset(merchant_id)
    except KeyError as exc:
        raise UnsupportedMerchantError(merchant_id) from exc
    return build_merchant_snapshot(dataset, channel=channel)


async def build_channel_export(merchant_id: str, channel: str = "ucp") -> ChannelReadinessReport:
    snapshot = await build_readiness_snapshot(merchant_id, channel=channel)
    if channel != "ucp":
        raise ValueError(f"Unsupported channel export: {channel}")
    return build_ucp_export(snapshot)


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

    resolved_psp = (
        str(psp_used or "").strip().lower()
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

    success, payment_intent, error, psp_used = await create_payment_with_failover(
        merchant_id=merchant_id,
        amount=amount,
        currency=currency,
        metadata=metadata,
        preferred_psps=preferred_psps,
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
    url = f"https://{shop_domain}/admin/api/2024-07/orders.json"
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
    if order_row and order_row.get("shopify_order_id"):
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
                event_payload={"shopify_order_id": order_row.get("shopify_order_id")},
            )
        if "state_synced" not in event_types:
            await journal.append_event(
                checkout_id=checkout_id,
                event_type="state_synced",
                event_payload={"status": "state_synced", "order_id": order_id},
            )
        updated = await journal.update_checkout_session(checkout_id, status="state_synced")
        return {"checkout": updated, "events": await journal.list_events(checkout_id), "replayed": replay}

    dataset = await load_merchant_source_dataset(merchant_id)
    merchant_connection = dataset.merchant_connection or {}
    shopify_conn = merchant_connection.get("shopify") or {}
    shop_domain = str(shopify_conn.get("shop_domain") or "").strip()
    access_token = str(shopify_conn.get("access_token") or "").strip()
    if not shop_domain or not access_token:
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="merchant_writeback_failed",
            event_payload={"code": "shopify_configuration_missing"},
        )
        updated = await journal.update_checkout_session(checkout_id, status="failed")
        return {"checkout": updated, "events": await journal.list_events(checkout_id), "replayed": "merchant_writeback_failed" in event_types}

    writeback = await _create_shopify_order_for_checkout(
        checkout=checkout,
        shop_domain=shop_domain,
        access_token=access_token,
    )
    if not writeback.get("ok"):
        await journal.append_event(
            checkout_id=checkout_id,
            event_type="merchant_writeback_failed",
            event_payload=writeback,
        )
        updated = await journal.update_checkout_session(checkout_id, status="failed")
        return {"checkout": updated, "events": await journal.list_events(checkout_id), "replayed": "merchant_writeback_failed" in event_types}

    await update_fulfillment_info(
        order_id=order_id,
        shopify_order_id=writeback.get("shopify_order_id"),
        fulfillment_status="processing",
    )
    await journal.append_event(
        checkout_id=checkout_id,
        event_type="order_forwarded_to_merchant",
        event_payload=writeback,
    )
    await journal.update_checkout_session(
        checkout_id,
        status="forwarded",
        session_payload_patch={"merchant_order": writeback},
    )

    payment_capabilities = dataset.payment_capabilities or {}
    external_payment_ref = payload.get("payment_reference")
    if external_payment_ref:
        try:
            await ensure_external_payment_transaction_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                shopify_order_id=str(writeback.get("shopify_order_id")),
                psp_used=payment_capabilities.get("psp_provider"),
                external_payment_ref=external_payment_ref,
                amount=float(((payload.get("price") or {}).get("amount")) or 0) * int(checkout.quantity or 1),
                currency=str(((payload.get("price") or {}).get("currency")) or "USD"),
                pivota_order_id=order_id,
            )
        except Exception:
            logger.warning("Shopify transaction sync failed for checkout=%s", checkout_id, exc_info=True)

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
