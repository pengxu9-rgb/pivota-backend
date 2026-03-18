from __future__ import annotations

from collections import Counter
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db.orders import create_order, get_order, update_fulfillment_info
from readiness.channel_exports.ucp import build_ucp_export
from readiness.flags import readiness_alpha_merchant_id
from readiness.models import ChannelReadinessReport, CheckoutSessionRecord, MerchantReadinessSnapshot, ReadyProduct, ReadyVariant
from readiness.order_sync import get_default_journal
from readiness.scoring import build_merchant_snapshot, find_ready_variant
from readiness.sources import load_merchant_source_dataset, supported_merchant_ids
from readiness.sync_audit import build_order_sync_audit_snapshot
from services.shopify_transactions_service import ensure_external_payment_transaction_best_effort

logger = logging.getLogger(__name__)


class UnsupportedMerchantError(KeyError):
    pass


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
