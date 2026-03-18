from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from readiness.models import CheckoutSessionRecord, OrderSyncEventRecord


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        out: Dict[str, Any] = {}
        keys = getattr(row, "keys", None)
        if callable(keys):
            for key in keys():
                out[str(key)] = row[key]
        return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _json_safe(dump())
    if hasattr(value, "dict") and callable(getattr(value, "dict")):
        return _json_safe(value.dict())
    return value


def _latest_at(rows: List[Dict[str, Any]], *keys: str) -> Optional[str]:
    for row in rows:
        for key in keys:
            value = row.get(key)
            if value:
                return str(_json_safe(value))
    return None


def _event_types(events: List[OrderSyncEventRecord]) -> List[str]:
    return [str(event.event_type) for event in events]


def _metadata_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _fetch_rows_best_effort(db: Any, query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        rows = await db.fetch_all(query, values)
        return [_json_safe(_row_to_dict(row)) for row in (rows or [])]
    except Exception:
        return []


async def build_order_sync_audit_snapshot(
    *,
    merchant_id: str,
    checkout: CheckoutSessionRecord,
    readiness_events: List[OrderSyncEventRecord],
    get_order_fn,
    db: Any,
    sample_limit: int = 10,
) -> Dict[str, Any]:
    payload = _json_safe(checkout.session_payload or {})
    order_id = str(checkout.order_id or "").strip() or None
    order_row = _json_safe(await get_order_fn(order_id)) if order_id else None
    order_state = _row_to_dict(order_row)
    shopify_order_id = str(order_state.get("shopify_order_id") or "").strip() or None
    readiness_event_types = _event_types(readiness_events)

    warnings: List[str] = []
    recommendations: List[str] = []

    order_events = await _fetch_rows_best_effort(
        db,
        """
        SELECT event_type, status, error_message, metadata, created_at
        FROM order_events
        WHERE merchant_id = :merchant_id AND order_id = :order_id
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "order_id": order_id, "limit": sample_limit},
    ) if order_id else []
    if order_id and not order_events:
        warnings.append("order_events_unobserved_or_unavailable")

    webhook_events = await _fetch_rows_best_effort(
        db,
        """
        SELECT topic, webhook_id, signature_verified, occurred_at, received_at
        FROM pcs_shopify_webhook_events
        WHERE merchant_id = :merchant_id
          AND (
            payload_json->>'id' = :shopify_order_id
            OR payload_json->>'order_id' = :shopify_order_id
            OR payload_json->>'shopify_order_id' = :shopify_order_id
          )
        ORDER BY received_at DESC, id DESC
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "shopify_order_id": shopify_order_id, "limit": sample_limit},
    ) if shopify_order_id else []
    if shopify_order_id and not webhook_events:
        warnings.append("shopify_webhooks_not_yet_observed")

    refund_records = await _fetch_rows_best_effort(
        db,
        """
        SELECT refund_id, amount, currency, status, error_message, platform_type, platform_refund_id, created_at, processed_at
        FROM refund_records
        WHERE merchant_id = :merchant_id AND order_id = :order_id
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "order_id": order_id, "limit": sample_limit},
    ) if order_id else []

    if order_id and shopify_order_id:
        return_records = await _fetch_rows_best_effort(
            db,
            """
            SELECT source_return_id, status, refund_status_raw, platform_order_id, updated_at, created_at
            FROM return_records
            WHERE merchant_id = :merchant_id
              AND (order_id = :order_id OR platform_order_id = :shopify_order_id)
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit
            """,
            {
                "merchant_id": merchant_id,
                "order_id": order_id,
                "shopify_order_id": shopify_order_id,
                "limit": sample_limit,
            },
        )
    elif order_id:
        return_records = await _fetch_rows_best_effort(
            db,
            """
            SELECT source_return_id, status, refund_status_raw, platform_order_id, updated_at, created_at
            FROM return_records
            WHERE merchant_id = :merchant_id AND order_id = :order_id
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit
            """,
            {
                "merchant_id": merchant_id,
                "order_id": order_id,
                "limit": sample_limit,
            },
        )
    else:
        return_records = []

    webhook_topics = [str(row.get("topic") or "") for row in webhook_events if row.get("topic")]
    order_event_types = [str(row.get("event_type") or "") for row in order_events if row.get("event_type")]
    refund_transaction_events = [
        {**row, "metadata": _metadata_dict(row)}
        for row in order_events
        if str(row.get("event_type") or "") == "readiness_refund_transaction_sync"
    ]
    latest_refund_transaction_event = refund_transaction_events[0] if refund_transaction_events else None
    latest_refund_transaction_metadata = _metadata_dict(latest_refund_transaction_event or {})
    latest_transaction_sync = latest_refund_transaction_metadata.get("transaction_sync")
    if not isinstance(latest_transaction_sync, dict):
        latest_transaction_sync = {}

    merchant_writeback_status = "pending"
    if shopify_order_id:
        merchant_writeback_status = "ready"
    elif "merchant_writeback_failed" in readiness_event_types or str(checkout.status or "") == "failed":
        merchant_writeback_status = "blocked"
    elif order_id:
        merchant_writeback_status = "pending"

    webhook_ingest_status = "not_observed"
    if shopify_order_id and webhook_events:
        webhook_ingest_status = "ready"
    elif shopify_order_id:
        webhook_ingest_status = "pending"
    elif merchant_writeback_status == "blocked":
        webhook_ingest_status = "blocked"

    cancellation_observed = (
        str(order_state.get("status") or "").lower() == "cancelled"
        or "orders/cancelled" in webhook_topics
        or "order_cancelled_webhook" in order_event_types
    )
    cancellation_status = "ready" if cancellation_observed else "not_observed"

    total_refunded = float(order_state.get("total_refunded") or 0)
    payment_status = str(order_state.get("payment_status") or "").lower()
    payment_reference = str(order_state.get("payment_intent_id") or "").strip()
    successful_refund_records = [
        row for row in refund_records if str(row.get("status") or "").strip().lower() in {"completed", "success"}
    ]
    failed_refund_records = [
        row for row in refund_records if str(row.get("status") or "").strip().lower() == "failed"
    ]
    refund_observed = (
        bool(successful_refund_records)
        or "refunds/create" in webhook_topics
        or "orders/refunded" in webhook_topics
        or payment_status in {"refunded", "partially_refunded"}
        or total_refunded > 0
    )
    paid_like = payment_status in {"paid", "completed", "succeeded", "settled", "partially_refunded", "refunded"}
    refund_eligible = bool(payment_reference) and paid_like
    if refund_observed:
        refund_status = "ready"
    elif failed_refund_records:
        refund_status = "failed"
    elif refund_eligible:
        refund_status = "not_observed"
    else:
        refund_status = "not_eligible"

    refund_transaction_mirror_status = "not_applicable"
    if latest_refund_transaction_event:
        refund_transaction_mirror_status = str(
            latest_refund_transaction_event.get("status")
            or (
                "soft_skipped"
                if latest_transaction_sync.get("soft_skipped")
                else ("ready" if latest_transaction_sync.get("ok") else "failed")
            )
        ).strip().lower() or "failed"
    elif refund_observed:
        refund_transaction_mirror_status = "not_observed"

    refund_transaction_mirror_reason = str(latest_transaction_sync.get("reason") or "").strip() or None

    return_observed = bool(return_records) or any(topic.startswith("returns/") for topic in webhook_topics)
    return_status = "ready" if return_observed else "not_observed"

    checkout_status = str(checkout.status or "").strip().lower()
    order_status = str(order_state.get("status") or "").strip().lower()
    if order_status in {"cancelled", "refunded"} and checkout_status and checkout_status != order_status:
        warnings.append("readiness_checkout_state_lags_order_state")
        recommendations.append("Replay /order-sync for this checkout so the readiness journal can absorb the downstream merchant order state.")

    if merchant_writeback_status == "blocked":
        recommendations.append("Inspect readiness journal for merchant_writeback_failed and verify Shopify credentials for the alpha merchant.")
    if merchant_writeback_status == "pending":
        recommendations.append("Order exists locally but merchant write-back has not been confirmed; rerun /order-sync before checking downstream sync.")
    if webhook_ingest_status == "pending":
        recommendations.append("Merchant write-back succeeded but no Shopify webhook has been observed yet; re-run this audit after webhook delivery or inspect pcs_shopify_webhook_events.")
    if cancellation_status == "not_observed":
        recommendations.append("Cancellation sync has not been exercised yet; trigger a controlled Shopify cancellation before treating this path as validated.")
    if refund_status == "not_observed":
        recommendations.append("Refund sync has not been exercised yet; trigger a controlled refund and verify refund_records plus order payment_status convergence.")
    if refund_status == "not_eligible":
        recommendations.append("Refund sync cannot be exercised on this readiness order yet; attach a paid payment reference or run real PSP execution before validating refunds.")
    if refund_status == "failed":
        warnings.append("canonical_refund_failed")
        recommendations.append(
            "Refund was attempted but canonical refund did not converge. Inspect refund_records.error_message, PSP state, and refund retry queue before retrying."
        )
    if refund_transaction_mirror_status == "soft_skipped":
        warnings.append("shopify_refund_transaction_mirror_degraded")
        recommendations.append(
            "Canonical refund succeeded, but Shopify refund transaction mirroring degraded. Treat this as a controlled Shopify limitation unless downstream reconciliation explicitly requires mirror transactions."
        )
    if refund_transaction_mirror_status == "failed":
        warnings.append("shopify_refund_transaction_mirror_failed")
        recommendations.append(
            "Canonical refund succeeded, but Shopify refund transaction mirroring failed. Inspect readiness_refund_transaction_sync order-event metadata and shopify_transactions_service behavior."
        )
    if return_status == "not_observed":
        recommendations.append("Return sync has not been exercised yet; trigger a controlled return/RMA and verify return_records upsert.")

    return {
        "merchant_id": merchant_id,
        "checkout_id": checkout.checkout_id,
        "merchant_alpha_mode": payload.get("merchant_alpha_mode"),
        "checkout_status": checkout.status,
        "order_id": order_id,
        "shopify_order_id": shopify_order_id,
        "source_of_truth": payload.get("source_of_truth") or {},
        "order_state": {
            "status": order_state.get("status"),
            "payment_status": order_state.get("payment_status"),
            "payment_intent_id": order_state.get("payment_intent_id"),
            "psp_used": order_state.get("psp_used"),
            "fulfillment_status": order_state.get("fulfillment_status"),
            "total_refunded": total_refunded,
            "updated_at": order_state.get("updated_at"),
        },
        "sync_signals": {
            "merchant_writeback": {
                "status": merchant_writeback_status,
                "shopify_order_id": shopify_order_id,
                "latest_readiness_event_at": _latest_at([_json_safe(event) for event in readiness_events], "created_at"),
            },
            "webhook_ingest": {
                "status": webhook_ingest_status,
                "event_count": len(webhook_events),
                "signature_verified_count": sum(1 for row in webhook_events if row.get("signature_verified") is True),
                "observed_topics": sorted(set(webhook_topics)),
                "latest_received_at": _latest_at(webhook_events, "received_at", "occurred_at"),
            },
            "cancellation_sync": {
                "status": cancellation_status,
                "observed_topics": sorted(topic for topic in set(webhook_topics) if topic == "orders/cancelled"),
                "observed_order_events": sorted(event for event in set(order_event_types) if event == "order_cancelled_webhook"),
            },
            "refund_sync": {
                "status": refund_status,
                "refund_eligible": refund_eligible,
                "eligibility_reason": (
                    None
                    if refund_eligible or refund_observed or refund_status == "failed"
                    else ("missing_payment_reference" if paid_like else "order_not_paid")
                ),
                "refund_record_count": len(refund_records),
                "successful_refund_record_count": len(successful_refund_records),
                "failed_refund_record_count": len(failed_refund_records),
                "latest_refund_record_status": refund_records[0].get("status") if refund_records else None,
                "latest_error_message": refund_records[0].get("error_message") if failed_refund_records else None,
                "total_refunded": total_refunded,
                "payment_status": order_state.get("payment_status"),
                "observed_topics": sorted(topic for topic in set(webhook_topics) if topic in {"refunds/create", "orders/refunded"}),
                "latest_refund_at": _latest_at(successful_refund_records or refund_records, "processed_at", "created_at"),
            },
            "refund_transaction_mirror": {
                "status": refund_transaction_mirror_status,
                "reason": refund_transaction_mirror_reason,
                "soft_skipped": refund_transaction_mirror_status == "soft_skipped",
                "event_count": len(refund_transaction_events),
                "transaction_id": latest_transaction_sync.get("transaction_id"),
                "parent_transaction_id": latest_transaction_sync.get("parent_transaction_id"),
                "psp_refund_id": latest_refund_transaction_metadata.get("psp_refund_id"),
                "platform_refund_id": latest_refund_transaction_metadata.get("platform_refund_id"),
                "latest_event_at": _latest_at(refund_transaction_events, "created_at"),
            },
            "return_sync": {
                "status": return_status,
                "return_record_count": len(return_records),
                "latest_return_status": return_records[0].get("status") if return_records else None,
                "observed_topics": sorted(topic for topic in set(webhook_topics) if topic.startswith("returns/")),
                "latest_return_at": _latest_at(return_records, "updated_at", "created_at"),
            },
        },
        "warnings": warnings,
        "recommendations": recommendations,
        "evidence": {
            "readiness_event_types": readiness_event_types,
            "order_events": order_events[:sample_limit],
            "webhook_events": webhook_events[:sample_limit],
            "refund_records": refund_records[:sample_limit],
            "return_records": return_records[:sample_limit],
            "sample_limit": sample_limit,
        },
    }
