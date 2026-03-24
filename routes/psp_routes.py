import asyncio
import base64
from datetime import datetime
from decimal import Decimal
import hmac
import hashlib
import json
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Request, Header, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from adapters.stripe_adapter import verify_webhook_signature
from config.settings import settings
from db.database import database
from db.orders import get_order, mark_order_paid, update_order_status, update_payment_info
from db.products import log_order_event
from orchestrator.callback_handler import handle_psp_webhook
from services.merchant_webhook_service import emit_merchant_webhook_event
from services.psp_payment_finalizer import (
    finalize_cancellation,
    finalize_payment_failure,
    finalize_payment_success,
    finalize_refund_failure,
    finalize_refund_success,
)
from utils.logger import logger

router = APIRouter(prefix="/psp", tags=["psp"])
security = HTTPBasic()


def _adyen_hmac_key_bytes(secret: str) -> bytes:
    raw = (secret or "").strip()
    if not raw:
        return b""
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode("utf-8")


def _adyen_escape_component(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace(":", "\\:")


def _adyen_notification_signing_string(notification: dict) -> str:
    amount = notification.get("amount") or {}
    parts = [
        notification.get("pspReference"),
        notification.get("originalReference"),
        notification.get("merchantAccountCode"),
        notification.get("merchantReference"),
        amount.get("value"),
        amount.get("currency"),
        notification.get("eventCode"),
        notification.get("success"),
    ]
    return ":".join(_adyen_escape_component(part) for part in parts)


def _verify_adyen_notification_hmac(notification: dict, webhook_secret: str) -> bool:
    additional_data = notification.get("additionalData") or {}
    provided_signature = additional_data.get("hmacSignature")
    if not provided_signature:
        return False
    payload = _adyen_notification_signing_string(notification).encode("utf-8")
    digest = hmac.new(_adyen_hmac_key_bytes(webhook_secret), payload, hashlib.sha256).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, provided_signature)


def _minor_unit_factor(currency: Optional[str]) -> Decimal:
    c = (currency or "").strip().lower()
    if not c:
        return Decimal("100")
    zero_decimal = {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
        "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
    three_decimal = {"bhd", "jod", "kwd", "omr", "tnd"}
    if c in zero_decimal:
        return Decimal("1")
    if c in three_decimal:
        return Decimal("1000")
    return Decimal("100")


def _decimal_money(value: object) -> Decimal:
    try:
        if value is None:
            return Decimal("0")
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _metadata_dict(order: Optional[dict]) -> dict:
    metadata = (order or {}).get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _refund_refs_list(metadata: dict) -> list[str]:
    raw = metadata.get("adyen_refund_psp_refs") or []
    if not isinstance(raw, list):
        return []
    refs: list[str] = []
    for item in raw:
        ref = str(item or "").strip()
        if ref:
            refs.append(ref)
    return refs


def _refund_records_dict(metadata: dict) -> dict:
    raw = metadata.get("adyen_refund_records") or {}
    return raw if isinstance(raw, dict) else {}


def _next_refund_status(order_total: Decimal, total_refunded: Decimal) -> str:
    if total_refunded <= Decimal("0"):
        return "paid"
    if order_total > Decimal("0") and total_refunded >= order_total:
        return "refunded"
    return "partially_refunded"


def _order_status_lower(order: dict) -> str:
    return str((order or {}).get("status") or "").strip().lower()


def _payment_status_lower(order: dict) -> str:
    return str((order or {}).get("payment_status") or "").strip().lower()


def _is_refund_like_order(order: dict) -> bool:
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    if payment_status in {"paid", "completed", "succeeded", "success", "settled", "partially_refunded", "refunded"}:
        return True
    return status in {"paid", "completed", "fulfilled", "partially_refunded", "refunded"}


def _is_cancellable_order(order: dict) -> bool:
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    if payment_status in {"pending", "authorized", "requires_capture", "awaiting_checkout", "checkout_in_progress"}:
        return True
    return status in {"draft", "pending", "authorized", "awaiting_checkout", "checkout_in_progress", "confirmed", "merchant_confirming"}


def _can_apply_authorisation_success(order: Optional[dict]) -> bool:
    if not order:
        return False
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    if _is_refund_like_order(order):
        return False
    if payment_status in {"payment_failed", "cancelled"}:
        return False
    if status in {"payment_failed", "cancelled"}:
        return False
    return True


async def _finalize_adyen_payment_success(order: dict, *, psp_reference: str, notification: dict) -> dict:
    return await finalize_payment_success(
        order,
        psp="adyen",
        payment_reference=psp_reference,
        transaction_id=psp_reference,
        amount_minor=notification.get("amount", {}).get("value"),
        currency=notification.get("amount", {}).get("currency"),
        metadata_extra={"event_code": notification.get("eventCode")},
        update_payment_info_fn=update_payment_info,
        mark_order_paid_fn=mark_order_paid,
        log_order_event_fn=log_order_event,
    )


async def _finalize_adyen_payment_failure(order: dict, *, psp_reference: str, notification: dict, source_event: str) -> dict:
    return await finalize_payment_failure(
        order,
        psp="adyen",
        payment_reference=psp_reference,
        error_message=notification.get("reason") or notification.get("eventCode") or "Adyen payment failure",
        source_event=source_event,
        record_generic_failure_metadata=source_event != "capture_failed_webhook",
        metadata_extra={
            "event_code": notification.get("eventCode"),
            "original_reference": notification.get("originalReference"),
            "reason": notification.get("reason"),
        },
        metadata_patch=(
            {
                "adyen_last_capture_failed": {
                    "psp_reference": psp_reference,
                    "original_reference": notification.get("originalReference"),
                    "reason": notification.get("reason"),
                    "received_at": datetime.now().isoformat(),
                }
            }
            if source_event == "capture_failed_webhook"
            else None
        ),
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


async def _finalize_adyen_refund_success(order: dict, *, psp_reference: str, notification: dict) -> dict:
    currency = notification.get("amount", {}).get("currency")
    refund_minor = _decimal_money(notification.get("amount", {}).get("value"))
    refund_amount = refund_minor / _minor_unit_factor(currency)
    return await finalize_refund_success(
        order,
        psp="adyen",
        refund_reference=psp_reference,
        refund_amount_minor=str(refund_minor),
        refund_amount=str(refund_amount),
        currency=currency,
        metadata_extra={"event_code": notification.get("eventCode")},
        metadata_patch={
            "adyen_refund_psp_refs": [*(_refund_refs_list(_metadata_dict(order))), psp_reference],
            "adyen_refund_records": {
                **_refund_records_dict(_metadata_dict(order)),
                psp_reference: {
                    "psp_reference": psp_reference,
                    "amount_minor": str(refund_minor),
                    "amount": str(refund_amount),
                    "currency": currency,
                    "received_at": datetime.now().isoformat(),
                },
            },
            "adyen_last_refund": {
                "psp_reference": psp_reference,
                "amount_minor": str(refund_minor),
                "amount": str(refund_amount),
                "currency": currency,
                "received_at": datetime.now().isoformat(),
            },
        },
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


async def _finalize_adyen_refund_failure(
    order: dict,
    *,
    psp_reference: str,
    rollback_reference: Optional[str],
    rollback_amount: Any,
    notification: dict,
    source_event: str,
) -> dict:
    metadata_patch = None
    if rollback_reference:
        failure_key = "adyen_last_refund_failure" if source_event == "refund_failed_webhook" else "adyen_last_refund_reversal"
        existing_metadata = _metadata_dict(order)
        existing_refs = _refund_refs_list(existing_metadata)
        existing_records = _refund_records_dict(existing_metadata)
        next_refs = [ref for ref in existing_refs if ref != rollback_reference]
        next_records = {key: value for key, value in existing_records.items() if key != rollback_reference}
        metadata_patch = {
            "adyen_refund_psp_refs": next_refs,
            "adyen_refund_records": next_records,
            failure_key: {
                "psp_reference": psp_reference,
                "rolled_back_ref": rollback_reference,
                "amount": str(rollback_amount) if rollback_amount is not None else None,
                "reason": notification.get("reason"),
                "received_at": datetime.now().isoformat(),
            }
        }
    return await finalize_refund_failure(
        order,
        psp="adyen",
        refund_reference=psp_reference,
        failure_reason=notification.get("reason") or notification.get("eventCode") or "Adyen refund failure",
        rollback_reference=rollback_reference,
        rollback_amount=rollback_amount,
        source_event=source_event,
        metadata_extra={
            "event_code": notification.get("eventCode"),
            "rolled_back_ref": rollback_reference,
            "refund_amount": str(rollback_amount) if rollback_amount is not None else None,
        },
        metadata_patch=metadata_patch,
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


async def _finalize_adyen_cancellation(order: dict, *, psp_reference: str, notification: dict, source_event: str) -> dict:
    return await finalize_cancellation(
        order,
        psp="adyen",
        cancel_reference=psp_reference,
        reason=notification.get("reason"),
        source_event=source_event,
        metadata_extra={"event_code": notification.get("eventCode")},
        metadata_patch={
            "adyen_last_cancellation": {
                "psp_reference": psp_reference,
                "received_at": datetime.now().isoformat(),
            }
        },
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


async def _lookup_refund_id_for_adyen_reference(
    *,
    merchant_id: str,
    order_id: str,
    psp_reference: str,
) -> str:
    try:
        row = await database.fetch_one(
            """
            SELECT refund_id
            FROM refund_records
            WHERE merchant_id = :merchant_id
              AND order_id = :order_id
              AND (
                  refund_id = :psp_reference
                  OR psp_refund_id = :psp_reference
                  OR platform_refund_id = :psp_reference
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "order_id": order_id,
                "psp_reference": psp_reference,
            },
        )
    except Exception as exc:
        logger.warning(
            "Failed to resolve Adyen refund_id for merchant=%s order=%s psp_reference=%s: %s",
            merchant_id,
            order_id,
            psp_reference,
            exc,
        )
        return ""
    if not row:
        return ""
    return str(row.get("refund_id") or row.get("refundID") or "").strip()


async def _emit_adyen_merchant_webhook_best_effort(
    order: dict,
    *,
    event_type: str,
    psp_reference: str,
    notification: dict,
    error_message: Optional[str] = None,
) -> None:
    merchant_id = str((order or {}).get("merchant_id") or "").strip()
    order_id = str((order or {}).get("order_id") or "").strip()
    if not merchant_id or not order_id:
        return

    amount_minor = notification.get("amount", {}).get("value")
    currency = str(notification.get("amount", {}).get("currency") or order.get("currency") or "")
    amount = None
    if amount_minor is not None:
        try:
            amount = float(_decimal_money(amount_minor) / _minor_unit_factor(currency))
        except Exception:
            amount = None

    if event_type == "refund.processed":
        order_total = _decimal_money(order.get("total"))
        current_total_refunded = _decimal_money(order.get("total_refunded"))
        next_total_refunded = current_total_refunded + _decimal_money(str(amount or 0))
        refund_status = _next_refund_status(order_total, next_total_refunded)
        payload = {
            "order_id": order_id,
            "merchant_id": merchant_id,
            "refund_id": await _lookup_refund_id_for_adyen_reference(
                merchant_id=merchant_id,
                order_id=order_id,
                psp_reference=psp_reference,
            ),
            "amount": amount,
            "currency": currency,
            "is_partial": refund_status == "partially_refunded",
            "status": refund_status,
        }
    else:
        payload = {
            "order_id": order_id,
            "merchant_id": merchant_id,
            "payment_id": psp_reference,
            "transaction_id": psp_reference,
            "amount": amount,
            "currency": currency,
            "psp_used": "adyen",
            "status": "paid" if event_type == "payment.completed" else "payment_failed",
            "customer_email": order.get("customer_email"),
        }
        if error_message:
            payload["error_message"] = error_message

    try:
        await emit_merchant_webhook_event(
            merchant_id,
            event_type=event_type,
            payload=payload,
        )
    except Exception as exc:
        logger.warning(
            "Failed to emit merchant Adyen webhook %s for %s: %s",
            event_type,
            merchant_id,
            exc,
        )

@router.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    logger.info("Stripe webhook received via compatibility alias /psp/webhook/stripe")
    from routes.webhook_routes import handle_stripe_webhook

    return await handle_stripe_webhook(request=request, stripe_signature=stripe_signature)


async def process_adyen_webhook_request(
    request: Request,
    credentials: HTTPBasicCredentials,
):
    """
    Canonical Adyen webhook processor shared by `/webhooks/adyen` and legacy `/psp/webhook/adyen`.
    """
    # Verify Basic Auth credentials
    adyen_username = settings.adyen_webhook_username if hasattr(settings, 'adyen_webhook_username') else "adyen_webhook_user"
    adyen_password = settings.adyen_webhook_password if hasattr(settings, 'adyen_webhook_password') else ""

    is_correct_username = secrets.compare_digest(credentials.username, adyen_username)
    is_correct_password = secrets.compare_digest(credentials.password, adyen_password)

    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    payload = await request.body()

    try:
        data = json.loads(payload)
        notification_items = data.get("notificationItems", [])

        if hasattr(settings, 'adyen_webhook_secret') and settings.adyen_webhook_secret:
            for item in notification_items:
                notification = item.get("NotificationRequestItem", {})
                if not _verify_adyen_notification_hmac(notification, settings.adyen_webhook_secret):
                    logger.warning(
                        "Adyen webhook HMAC verification failed for merchantReference=%s pspReference=%s",
                        notification.get("merchantReference"),
                        notification.get("pspReference"),
                    )
                    raise HTTPException(status_code=401, detail="Invalid HMAC signature")

        for item in notification_items:
            notification = item.get("NotificationRequestItem", {})

            # Extract event details
            event_code = notification.get("eventCode")
            success = notification.get("success")
            psp_reference = notification.get("pspReference")
            merchant_reference = notification.get("merchantReference")

            logger.info(f"Adyen webhook received: {event_code}, success={success}, ref={psp_reference}")

            # Handle different event types
            if event_code == "AUTHORISATION" and success == "true":
                logger.info(
                    f"[AdyenWebhook] handling success for {merchant_reference} psp_ref={psp_reference}"
                )
                # Try fetch order up-front to see if it exists
                try:
                    fetched = await get_order(merchant_reference)
                    logger.info(
                        f"[AdyenWebhook] fetched order for {merchant_reference}: {fetched}"
                    )
                except Exception as fetch_err:
                    logger.error(
                        f"[AdyenWebhook] get_order failed for {merchant_reference}: {fetch_err}"
                    )

                # Update PSP transactions (legacy/prototype)
                await handle_psp_webhook(
                    merchant_reference,
                    "succeeded",
                    "adyen",
                    psp_reference,
                )

                # Also mark corresponding order as paid.
                # We treat merchantReference as our internal order_id.
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if order and _can_apply_authorisation_success(order):
                        finalization = await _finalize_adyen_payment_success(
                            order,
                            psp_reference=psp_reference,
                            notification=notification,
                        )
                        if finalization.get("applied"):
                            logger.info(
                                f"Order {order_id} marked as paid via Adyen webhook"
                            )
                            await _emit_adyen_merchant_webhook_best_effort(
                                order,
                                event_type="payment.completed",
                                psp_reference=psp_reference,
                                notification=notification,
                            )
                        try:
                            from routes.order_routes import create_shopify_order

                            asyncio.create_task(create_shopify_order(order_id))
                        except Exception as shopify_err:
                            logger.warning(
                                f"[AdyenWebhook] create_shopify_order best-effort failed for {order_id}: {shopify_err}"
                            )
                    elif not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for {order_id}; cannot mark paid"
                        )
                    else:
                        logger.info(
                            f"[AdyenWebhook] order {order_id} already settled or in terminal state, skipping authorisation replay"
                        )
                except Exception as order_err:
                    logger.error(
                        f"Adyen webhook order update failed for {merchant_reference}: {order_err}"
                    )

            elif event_code == "AUTHORISATION" and success == "false":
                await handle_psp_webhook(
                    merchant_reference, "failed", "adyen", psp_reference
                )
                try:
                    order = await get_order(merchant_reference)
                    if order:
                        finalization = await _finalize_adyen_payment_failure(
                            order,
                            psp_reference=psp_reference,
                            notification=notification,
                            source_event="payment_failed_webhook",
                        )
                        if finalization.get("applied"):
                            await _emit_adyen_merchant_webhook_best_effort(
                                order,
                                event_type="payment.failed",
                                psp_reference=psp_reference,
                                notification=notification,
                                error_message=notification.get("reason") or notification.get("eventCode"),
                            )
                except Exception as authorisation_failure_err:
                    logger.error(
                        f"Adyen authorisation failure webhook order update failed for {merchant_reference}: {authorisation_failure_err}"
                    )
            elif event_code == "CAPTURE_FAILED":
                await handle_psp_webhook(
                    merchant_reference, "failed", "adyen", psp_reference
                )
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for capture_failed {order_id}; cannot reconcile"
                        )
                        continue

                    finalization = await _finalize_adyen_payment_failure(
                        order,
                        psp_reference=psp_reference,
                        notification=notification,
                        source_event="capture_failed_webhook",
                    )
                    if finalization.get("applied"):
                        await _emit_adyen_merchant_webhook_best_effort(
                            order,
                            event_type="payment.failed",
                            psp_reference=psp_reference,
                            notification=notification,
                            error_message=notification.get("reason") or notification.get("eventCode"),
                        )
                except Exception as capture_failed_err:
                    logger.error(
                        f"Adyen capture_failed webhook order update failed for {merchant_reference}: {capture_failed_err}"
                    )
            elif event_code in {"REFUND", "REFUND_WITH_DATA"} and success == "true":
                await handle_psp_webhook(
                    merchant_reference, "refunded", "adyen", psp_reference
                )
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for refund {order_id}; cannot reconcile refund"
                        )
                        continue

                    finalization = await _finalize_adyen_refund_success(
                        order,
                        psp_reference=psp_reference,
                        notification=notification,
                    )
                    if not finalization.get("applied"):
                        logger.info(
                            f"[AdyenWebhook] refund webhook already applied or skipped for {order_id} psp_ref={psp_reference}"
                        )
                    else:
                        await _emit_adyen_merchant_webhook_best_effort(
                            order,
                            event_type="refund.processed",
                            psp_reference=psp_reference,
                            notification=notification,
                        )
                except Exception as refund_err:
                    logger.error(
                        f"Adyen refund webhook order update failed for {merchant_reference}: {refund_err}"
                    )
            elif event_code in {"REFUND", "REFUND_WITH_DATA"} and success == "false":
                await handle_psp_webhook(
                    merchant_reference, "failed", "adyen", psp_reference
                )
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for failed refund {order_id}; cannot log failure"
                        )
                        continue
                    await log_order_event(
                        event_type="refund_rejected_webhook",
                        order_id=order_id,
                        merchant_id=order["merchant_id"],
                        metadata={
                            "psp": "adyen",
                            "payment_intent_id": psp_reference,
                            "reason": notification.get("reason"),
                            "event_code": event_code,
                        },
                    )
                except Exception as refund_reject_err:
                    logger.error(
                        f"Adyen failed refund webhook processing failed for {merchant_reference}: {refund_reject_err}"
                    )
            elif event_code in {"REFUND_FAILED", "REFUNDED_REVERSED"} and success == "true":
                await handle_psp_webhook(
                    merchant_reference,
                    "failed" if event_code == "REFUND_FAILED" else "reversed",
                    "adyen",
                    psp_reference,
                )
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for refund rollback {order_id}; cannot reconcile"
                        )
                        continue

                    metadata = _metadata_dict(order)
                    seen_refs = _refund_refs_list(metadata)
                    refund_records = _refund_records_dict(metadata)
                    rollback_ref = psp_reference
                    refund_record = refund_records.get(rollback_ref)
                    if not refund_record and event_code == "REFUNDED_REVERSED":
                        last_refund = metadata.get("adyen_last_refund") or {}
                        last_ref = str(last_refund.get("psp_reference") or "").strip()
                        if last_ref and last_ref in seen_refs:
                            rollback_ref = last_ref
                            refund_record = refund_records.get(last_ref) or last_refund
                    if not refund_record or rollback_ref not in seen_refs:
                        logger.info(
                            f"[AdyenWebhook] no applied refund found to roll back for {order_id} event={event_code} psp_ref={psp_reference}"
                        )
                        continue

                    refund_amount = _decimal_money(refund_record.get("amount"))
                    await _finalize_adyen_refund_failure(
                        order,
                        psp_reference=psp_reference,
                        rollback_reference=rollback_ref,
                        rollback_amount=str(refund_amount),
                        notification=notification,
                        source_event="refund_failed_webhook" if event_code == "REFUND_FAILED" else "refund_reversed_webhook",
                    )
                except Exception as refund_rollback_err:
                    logger.error(
                        f"Adyen refund rollback webhook order update failed for {merchant_reference}: {refund_rollback_err}"
                    )
            elif event_code == "CANCEL_OR_REFUND":
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if success != "true":
                        await handle_psp_webhook(order_id, "failed", "adyen", psp_reference)
                        if order:
                            await log_order_event(
                                event_type="cancel_or_refund_rejected_webhook",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "psp": "adyen",
                                    "payment_intent_id": psp_reference,
                                    "event_code": event_code,
                                    "reason": notification.get("reason"),
                                },
                            )
                        continue

                    if not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for cancel_or_refund {order_id}; cannot reconcile"
                        )
                        continue

                    metadata = _metadata_dict(order)
                    order_total = _decimal_money(order.get("total"))
                    current_total_refunded = _decimal_money(order.get("total_refunded"))
                    outcome = "ambiguous"

                    if _is_refund_like_order(order):
                        await handle_psp_webhook(order_id, "refunded", "adyen", psp_reference)
                        if current_total_refunded >= order_total and order_total > Decimal("0"):
                            logger.info(
                                f"[AdyenWebhook] cancel_or_refund already converged to refunded for {order_id}"
                            )
                            continue
                        await update_order_status(
                            order_id,
                            "refunded",
                            payment_status="refunded",
                            total_refunded=order_total,
                            metadata={
                                **metadata,
                                "adyen_last_cancel_or_refund": {
                                    "psp_reference": psp_reference,
                                    "outcome": "refunded",
                                    "reason": notification.get("reason"),
                                    "received_at": datetime.now().isoformat(),
                                },
                            },
                        )
                        outcome = "refunded"
                    elif _is_cancellable_order(order):
                        await handle_psp_webhook(order_id, "cancelled", "adyen", psp_reference)
                        if _order_status_lower(order) == "cancelled":
                            logger.info(
                                f"[AdyenWebhook] cancel_or_refund already converged to cancelled for {order_id}"
                            )
                            continue
                        await update_order_status(
                            order_id,
                            "cancelled",
                            payment_status="cancelled",
                            cancelled_at=datetime.now(),
                            metadata={
                                **metadata,
                                "adyen_last_cancel_or_refund": {
                                    "psp_reference": psp_reference,
                                    "outcome": "cancelled",
                                    "reason": notification.get("reason"),
                                    "received_at": datetime.now().isoformat(),
                                },
                            },
                        )
                        outcome = "cancelled"
                    else:
                        await handle_psp_webhook(order_id, "cancel_or_refund", "adyen", psp_reference)

                    await log_order_event(
                        event_type="cancel_or_refund_webhook",
                        order_id=order_id,
                        merchant_id=order["merchant_id"],
                        metadata={
                            "psp": "adyen",
                            "payment_intent_id": psp_reference,
                            "event_code": event_code,
                            "outcome": outcome,
                            "reason": notification.get("reason"),
                        },
                    )
                except Exception as cancel_or_refund_err:
                    logger.error(
                        f"Adyen cancel_or_refund webhook order update failed for {merchant_reference}: {cancel_or_refund_err}"
                    )
            elif event_code == "CANCELLATION" and success == "true":
                await handle_psp_webhook(
                    merchant_reference, "cancelled", "adyen", psp_reference
                )
                try:
                    order_id = merchant_reference
                    order = await get_order(order_id)
                    if not order:
                        logger.error(
                            f"[AdyenWebhook] no order found for cancellation {order_id}; cannot mark cancelled"
                        )
                        continue
                    finalization = await _finalize_adyen_cancellation(
                        order,
                        psp_reference=psp_reference,
                        notification=notification,
                        source_event="order_cancelled_webhook",
                    )
                    if not finalization.get("applied"):
                        logger.info(
                            f"[AdyenWebhook] order {order_id} already cancelled, skipping"
                        )
                except Exception as cancellation_err:
                    logger.error(
                        f"Adyen cancellation webhook order update failed for {merchant_reference}: {cancellation_err}"
                    )
            # Add more event types as needed

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Adyen webhook processing error: {e}")
        raise HTTPException(status_code=400, detail=f"Webhook processing failed: {str(e)}")

    # Adyen expects [accepted] response
    return {"notificationResponse": "[accepted]"}

@router.post("/webhook/adyen")
async def adyen_webhook(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security)
):
    logger.info("Adyen webhook received via compatibility alias /psp/webhook/adyen")
    return await process_adyen_webhook_request(request, credentials)
