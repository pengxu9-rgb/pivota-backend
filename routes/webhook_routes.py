"""
Webhook 处理路由
处理来自 PSP（Stripe/Adyen）和 MCP（Shopify）的事件通知
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Header, Response, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional, Dict, Any
from urllib.parse import urlparse
import stripe
import os
import hmac
import json
import socket
from datetime import datetime
from decimal import Decimal

from db.orders import get_order, update_order, update_order_status, mark_order_paid, mark_order_shipped
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from config.settings import settings
from utils.logger import logger
from services.dispute_records_service import stripe_dispute_pack_status
from services.shopify_webhook_ingest import verify_shopify_hmac, ingest_shopify_webhook
from services.catalog_sync_service import (
    create_catalog_sync_job,
    mark_catalog_sync_event_processed,
    record_catalog_sync_event,
    run_catalog_sync_job,
)
from services.shopify_products_sync import sync_shopify_products_for_merchant
from services.pcs_evidence_pack_service import create_order_snapshot_evidence_pack
from services.merchant_webhook_service import emit_merchant_webhook_event
from services.psp_payment_finalizer import (
    finalize_payment_failure,
    finalize_payment_success,
    finalize_refund_failure,
    finalize_refund_success,
)
from services.refund_observability import (
    extract_stripe_refund_snapshot,
    merge_refund_metadata,
    stripe_refund_metadata_patch,
)
from observability.reviews_metrics import record_shopify_webhook
from routes.reviews_invitation_issuer import (
    SendInvitationEmailFromOrderRequest,
    _internal_key as _reviews_invitation_internal_key,
    send_invitation_email_from_order,
    _invitation_send_delay_seconds as _reviews_invitation_send_delay_seconds,
    enqueue_invitation_email_send_job_from_order,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
adyen_webhook_security = HTTPBasic()


async def _run_shopify_catalog_webhook_reconcile(
    *,
    merchant_id: str,
    job_id: str,
    event_id: Optional[str],
    limit: int = 5000,
) -> None:
    try:
        await sync_shopify_products_for_merchant(
            merchant_id=merchant_id,
            limit=limit,
            ingest_catalog=False,
        )
        await run_catalog_sync_job(job_id)
        if event_id:
            await mark_catalog_sync_event_processed(event_id, status="processed")
    except Exception as exc:  # pragma: no cover - background task
        if event_id:
            await mark_catalog_sync_event_processed(event_id, status="failed", error_message=str(exc))
        logger.exception(
            "Shopify catalog webhook reconcile failed merchant=%s job_id=%s event_id=%s err=%s",
            merchant_id,
            job_id,
            event_id,
            exc,
        )

def _reviews_invitation_auto_send_on_shopify_fulfillment_enabled() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_AUTO_SEND_ON_SHOPIFY_FULFILLMENT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _stripe_minor_unit_factor(currency: Optional[str]) -> Decimal:
    """
    Stripe reports amounts in the smallest currency unit.
    Default exponent=2 (factor=100), with common exceptions handled.
    """
    c = (currency or "").strip().lower()
    if not c:
        return Decimal("100")

    zero_decimal = {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
    three_decimal = {"bhd", "jod", "kwd", "omr", "tnd"}

    if c in zero_decimal:
        return Decimal("1")
    if c in three_decimal:
        return Decimal("1000")
    return Decimal("100")


def _stripe_next_refund_status(order_total: Decimal, total_refunded: Decimal) -> str:
    if total_refunded <= Decimal("0"):
        return "paid"
    if order_total > Decimal("0") and total_refunded >= order_total:
        return "refunded"
    return "partially_refunded"


def _stripe_order_status_lower(order: Optional[Dict[str, Any]]) -> str:
    return str((order or {}).get("status") or "").strip().lower()


def _stripe_payment_status_lower(order: Optional[Dict[str, Any]]) -> str:
    return str((order or {}).get("payment_status") or "").strip().lower()


def _stripe_metadata_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _can_apply_stripe_payment_success(order: Optional[Dict[str, Any]]) -> bool:
    if not order:
        return False

    payment_status = _stripe_payment_status_lower(order)
    status = _stripe_order_status_lower(order)
    if payment_status in {
        "paid",
        "completed",
        "succeeded",
        "success",
        "settled",
        "partially_refunded",
        "refunded",
        "cancelled",
    }:
        return False
    if status in {"paid", "completed", "fulfilled", "partially_refunded", "refunded", "cancelled"}:
        return False
    try:
        if Decimal(str(order.get("total_refunded") or "0")) > Decimal("0"):
            return False
    except Exception:
        pass
    return True


def _can_apply_stripe_payment_failure(order: Optional[Dict[str, Any]]) -> bool:
    if not order:
        return False

    payment_status = _stripe_payment_status_lower(order)
    status = _stripe_order_status_lower(order)
    if payment_status in {
        "partially_refunded",
        "refunded",
        "cancelled",
    }:
        return False
    if status in {"partially_refunded", "refunded", "cancelled"}:
        return False
    try:
        if Decimal(str(order.get("total_refunded") or "0")) > Decimal("0"):
            return False
    except Exception:
        pass
    return True


async def _resolve_stripe_order_for_refund(
    *,
    payment_intent_id: Optional[str],
    refund_meta: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
    from db.database import database

    if payment_intent_id:
        result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
        if result:
            return result

    if isinstance(refund_meta, dict):
        order_hint = str(refund_meta.get("order_id") or "").strip()
        if order_hint:
            return await get_order(order_hint)
    return None


async def _persist_stripe_refund_observability(
    order: Optional[Dict[str, Any]],
    refund_snapshot: Optional[Dict[str, Any]],
) -> None:
    if not order or not refund_snapshot:
        return
    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        return
    metadata = merge_refund_metadata(
        order.get("metadata"),
        stripe_refund_metadata_patch(
            refund_snapshot,
            existing_metadata=order.get("metadata"),
        ),
    )
    await update_order(order_id, {"metadata": metadata})


async def _resolve_stripe_order_for_payment_event(
    *,
    payment_intent_id: Optional[str],
    payment_meta: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
    from db.database import database

    if payment_intent_id:
        result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
        if result:
            return result

    order_hint = ""
    if isinstance(payment_meta, dict):
        order_hint = str(payment_meta.get("order_id") or "").strip()
    if not order_hint:
        return None

    order = await get_order(order_hint)
    if not order:
        return None

    current_payment_intent_id = str(order.get("payment_intent_id") or "").strip()
    if payment_intent_id and current_payment_intent_id != payment_intent_id:
        try:
            await update_order(
                order_hint,
                {
                    "payment_intent_id": payment_intent_id,
                    "psp_used": "stripe",
                },
            )
            refreshed = await get_order(order_hint)
            if refreshed:
                return refreshed
        except Exception:
            pass

    return order


async def _finalize_stripe_payment_success(
    order: Dict[str, Any],
    *,
    payment_intent_id: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    return await finalize_payment_success(
        order,
        psp="stripe",
        payment_reference=payment_intent_id,
        transaction_id=payment_intent_id,
        amount_minor=data.get("amount"),
        currency=data.get("currency"),
        mark_order_paid_fn=mark_order_paid,
        log_order_event_fn=log_order_event,
    )


async def _finalize_stripe_payment_failure(
    order: Dict[str, Any],
    *,
    payment_intent_id: str,
    error_message: str,
) -> Dict[str, Any]:
    if not _can_apply_stripe_payment_failure(order):
        return {"applied": False, "reason": "terminal_state", "order_id": order.get("order_id")}
    return await finalize_payment_failure(
        order,
        psp="stripe",
        payment_reference=payment_intent_id,
        error_message=error_message,
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


async def _finalize_stripe_refund_success(
    order: Dict[str, Any],
    *,
    refund_reference: str,
    refund_amount_minor: Any,
    currency: Optional[str],
    refund_total: Decimal,
    metadata_extra: Optional[Dict[str, Any]] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return await finalize_refund_success(
        order,
        psp="stripe",
        refund_reference=refund_reference,
        refund_amount_minor=refund_amount_minor,
        refund_total=refund_total,
        currency=currency,
        metadata_extra=metadata_extra,
        metadata_patch=metadata_patch,
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


async def _finalize_stripe_refund_failure(
    order: Dict[str, Any],
    *,
    refund_reference: str,
    refund_amount_minor: Any,
    currency: Optional[str],
    failure_reason: str,
    refund_snapshot: Optional[Dict[str, Any]] = None,
    metadata_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rollback_amount = _stripe_minor_unit_factor(currency or str(order.get("currency") or ""))
    try:
        rollback_total = Decimal(str(refund_amount_minor or "0")) / rollback_amount
    except Exception:
        rollback_total = Decimal("0")
    refund_key = f"stripe:{refund_reference}"
    existing_metadata = order.get("metadata") or {}
    if not isinstance(existing_metadata, dict):
        existing_metadata = {}
    existing_refs = list(existing_metadata.get("psp_refund_refs") or [])
    legacy_match = str(existing_metadata.get("refund_id") or "").strip() == str(refund_reference or "").strip()
    failure_payload = {
        "refund_id": refund_reference,
        "amount_minor": refund_amount_minor,
        "currency": currency or str(order.get("currency") or ""),
        "failure_reason": failure_reason,
        "received_at": datetime.now().isoformat(),
        **(metadata_extra or {}),
    }
    if refund_snapshot:
        failure_payload.update(refund_snapshot)
    metadata_patch = {
        "stripe_last_refund_failure": failure_payload,
    }
    if refund_snapshot:
        metadata_patch.update(
            stripe_refund_metadata_patch(
                refund_snapshot,
                existing_metadata=existing_metadata,
            )
        )
    if not (refund_key in existing_refs or legacy_match):
        order_id = str(order.get("order_id") or "")
        merchant_id = str(order.get("merchant_id") or "")
        await update_order(
            order_id,
            {
                "metadata": merge_refund_metadata(
                    existing_metadata,
                    metadata_patch,
                )
            },
        )
        await log_order_event(
            event_type="refund_failed_webhook",
            order_id=order_id,
            merchant_id=merchant_id,
            metadata={
                "psp": "stripe",
                "payment_intent_id": str(refund_reference or "").strip(),
                "failure_reason": failure_reason,
                "rollback_applied": False,
                "rollback_reference": None,
                "next_total_refunded": str(order.get("total_refunded") or "0"),
                "refund_id": refund_reference,
                **(metadata_extra or {}),
            },
        )
        return {
            "applied": True,
            "rolled_back": False,
            "order_id": order_id,
            "merchant_id": merchant_id,
            "total_refunded": Decimal(str(order.get("total_refunded") or "0")),
            "next_status": str(order.get("payment_status") or ""),
        }
    return await finalize_refund_failure(
        order,
        psp="stripe",
        refund_reference=refund_reference,
        failure_reason=failure_reason,
        rollback_reference=refund_reference if (refund_key in existing_refs or legacy_match) else None,
        rollback_amount=rollback_total,
        metadata_extra={"refund_id": refund_reference, **(metadata_extra or {})},
        metadata_patch=metadata_patch,
        update_order_status_fn=update_order_status,
        log_order_event_fn=log_order_event,
    )


def _canonicalize_shop_domain(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").strip().lower()
        return host or None
    except Exception:
        return raw.lower()


async def _stripe_webhook_secret_candidates(psp_id: Optional[str]) -> list[str]:
    candidates: list[str] = []

    if psp_id:
        try:
            from db.database import database

            row = await database.fetch_one(
                """
                SELECT provider_config
                FROM merchant_psps
                WHERE psp_id = :psp_id AND provider = 'stripe'
                LIMIT 1
                """,
                {"psp_id": psp_id},
            )
            if row:
                raw_provider_config = row["provider_config"]
                provider_config: Dict[str, Any] = {}
                if isinstance(raw_provider_config, dict):
                    provider_config = dict(raw_provider_config)
                elif isinstance(raw_provider_config, str):
                    try:
                        parsed_provider_config = json.loads(raw_provider_config)
                        if isinstance(parsed_provider_config, dict):
                            provider_config = dict(parsed_provider_config)
                    except Exception:
                        provider_config = {}
                merchant_secret = str(provider_config.get("webhook_endpoint_secret") or "").strip()
                if merchant_secret:
                    candidates.append(merchant_secret)
        except Exception as exc:
            logger.warning("Failed to load merchant Stripe webhook secret for psp_id=%s: %s", psp_id, exc)

    global_secret = str(getattr(settings, "stripe_webhook_secret", "") or "").strip()
    if global_secret and global_secret not in candidates:
        candidates.append(global_secret)

    return candidates


def _stripe_object_to_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stripe_object_to_dict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stripe_object_to_dict(item) for item in value]
    for method_name in ("to_dict_recursive", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _stripe_object_to_dict(method())
            except Exception:
                continue
    return value


async def _emit_stripe_merchant_webhook_best_effort(
    order: Dict[str, Any],
    *,
    event_type: str,
    payment_intent_id: Optional[str] = None,
    amount_minor: Any = None,
    currency: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    merchant_id = str((order or {}).get("merchant_id") or "").strip()
    order_id = str((order or {}).get("order_id") or "").strip()
    if not merchant_id or not order_id:
        return

    resolved_currency = currency or str(order.get("currency") or "")
    amount = None
    if amount_minor is not None:
        try:
            amount = float(
                Decimal(str(amount_minor))
                / _stripe_minor_unit_factor(resolved_currency)
            )
        except Exception:
            amount = None

    payload = {
        "order_id": order_id,
        "merchant_id": merchant_id,
        "payment_id": payment_intent_id,
        "transaction_id": payment_intent_id,
        "amount": amount,
        "currency": resolved_currency,
        "psp_used": "stripe",
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
            "Failed to emit merchant Stripe webhook %s for %s: %s",
            event_type,
            merchant_id,
            exc,
        )


# ============================================================================
# Stripe Webhooks
# ============================================================================

@router.post("/stripe/{psp_id}")
@router.post("/stripe")
async def handle_stripe_webhook(
    request: Request,
    psp_id: Optional[str] = None,
    stripe_signature: Optional[str] = Header(None)
):
    """
    处理 Stripe 支付事件
    
    支持的事件：
    - payment_intent.succeeded: 支付成功
    - payment_intent.payment_failed: 支付失败
    - refund.created / refund.updated / refund.failed: 退款时间线
    - charge.refunded: 退款成功
    - charge.dispute.*: 争议/拒付（chargeback）信号（best-effort 记录，不自动变更订单状态）
    """
    try:
        payload = await request.body()
        event = None

        secret_candidates = await _stripe_webhook_secret_candidates(psp_id)
        if secret_candidates:
            last_signature_error: Optional[Exception] = None
            for webhook_secret in secret_candidates:
                try:
                    event = stripe.Webhook.construct_event(payload, stripe_signature, webhook_secret)
                    break
                except ValueError:
                    logger.error("Invalid Stripe webhook payload")
                    raise HTTPException(status_code=400, detail="Invalid payload")
                except Exception as exc:
                    last_signature_error = exc
                    continue
            if event is None:
                logger.error(
                    "Invalid Stripe webhook signature for psp_id=%s after trying %s candidate(s): %s",
                    psp_id,
                    len(secret_candidates),
                    last_signature_error,
                )
                raise HTTPException(status_code=400, detail="Invalid signature")
        else:
            # 开发环境：不验证签名
            event = json.loads(payload)
        event = _stripe_object_to_dict(event)
        if not isinstance(event, dict):
            logger.error("Invalid Stripe webhook event shape: %s", type(event).__name__)
            raise HTTPException(status_code=400, detail="Invalid event")
        
        # 处理事件
        event_type = event.get("type")
        data_container = _stripe_object_to_dict(event.get("data") or {})
        data = (
            _stripe_object_to_dict(data_container.get("object"))
            if isinstance(data_container, dict)
            else {}
        )
        if not isinstance(data, dict):
            data = {}
        
        logger.info(f"Received Stripe webhook: {event_type}")
        
        if event_type == "payment_intent.succeeded":
            # 支付成功
            payment_intent_id = data.get("id")
            payment_meta = _stripe_object_to_dict(data.get("metadata") or {})
            result = await _resolve_stripe_order_for_payment_event(
                payment_intent_id=payment_intent_id,
                payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
            )
            
            if result:
                order_id = result["order_id"]
                merchant_id = result["merchant_id"]
                finalization = await _finalize_stripe_payment_success(
                    result,
                    payment_intent_id=payment_intent_id,
                    data=data,
                )
                if finalization.get("applied"):
                    logger.info(f"Order {order_id} marked as paid via webhook")
                    await _emit_stripe_merchant_webhook_best_effort(
                        result,
                        event_type="payment.completed",
                        payment_intent_id=payment_intent_id,
                        amount_minor=data.get("amount"),
                        currency=data.get("currency"),
                    )

                    # PCS: freeze order snapshot evidence (best-effort; does not block payment success)
                    try:
                        await create_order_snapshot_evidence_pack(order_id, triggered_by="stripe_webhook")
                    except Exception as e:
                        logger.warning(f"PCS evidence snapshot failed for {order_id}: {e}")

                    order_metadata = result.get("metadata") or {}
                    if not isinstance(order_metadata, dict):
                        order_metadata = {}
                    skip_platform_order_creation = (
                        _stripe_metadata_flag(order_metadata.get("skip_platform_order_creation"))
                        or _stripe_metadata_flag(order_metadata.get("ops_canary"))
                        or _stripe_metadata_flag((payment_meta or {}).get("skip_platform_order_creation"))
                        or _stripe_metadata_flag((payment_meta or {}).get("ops_canary"))
                    )

                    if not skip_platform_order_creation:
                        # 触发 Shopify 订单创建
                        from routes.order_routes import create_shopify_order

                        store_info = await get_primary_store(merchant_id)
                        if store_info and store_info.get("platform") == "shopify":
                            logger.info(f"🔄 Creating Shopify order for {order_id} after webhook payment confirmation")
                            try:
                                success = await create_shopify_order(order_id)
                                if success:
                                    logger.info(f"✅ Shopify order created via webhook for {order_id}")
                                else:
                                    logger.error(f"❌ Shopify order creation failed for {order_id}")
                            except Exception as shop_err:
                                logger.error(f"❌ Shopify order creation error: {shop_err}")
                else:
                    logger.info(
                        "Stripe payment success replay skipped for order %s due to settled or terminal state",
                        order_id,
                    )
                
        elif event_type == "payment_intent.payment_failed":
            # 支付失败
            payment_intent_id = data.get("id")
            last_payment_error = _stripe_object_to_dict(data.get("last_payment_error") or {})
            error_message = (
                last_payment_error.get("message", "Unknown error")
                if isinstance(last_payment_error, dict)
                else "Unknown error"
            )
            payment_meta = _stripe_object_to_dict(data.get("metadata") or {})
            result = await _resolve_stripe_order_for_payment_event(
                payment_intent_id=payment_intent_id,
                payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
            )
            
            if result:
                order_id = result["order_id"]
                finalization = await _finalize_stripe_payment_failure(
                    result,
                    payment_intent_id=payment_intent_id,
                    error_message=error_message,
                )
                if finalization.get("applied"):
                    logger.warning(f"Order {order_id} payment failed: {error_message}")
                    await _emit_stripe_merchant_webhook_best_effort(
                        result,
                        event_type="payment.failed",
                        payment_intent_id=payment_intent_id,
                        amount_minor=data.get("amount"),
                        currency=data.get("currency"),
                        error_message=error_message,
                    )
                else:
                    logger.info(
                        "Stripe payment failure replay skipped for order %s due to settled or terminal state",
                        order_id,
                    )
                
        elif event_type == "charge.refunded":
            # 退款成功
            charge_id = data.get("id")
            payment_intent_id = data.get("payment_intent")
            refund_amount = data.get("amount_refunded")
            currency = (data.get("currency") or "").strip().lower() or None
            
            query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
            from db.database import database
            result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
            
            if result:
                order_id = result["order_id"]
                try:
                    refunded_minor = Decimal(str(refund_amount)) if refund_amount is not None else Decimal("0")
                except Exception:
                    refunded_minor = Decimal("0")

                factor = _stripe_minor_unit_factor(currency or str(result.get("currency") or ""))
                try:
                    refunded_total = refunded_minor / factor
                except Exception:
                    refunded_total = Decimal("0")
                await _finalize_stripe_refund_success(
                    result,
                    refund_reference=charge_id,
                    refund_amount_minor=refund_amount,
                    currency=currency or str(result.get("currency") or ""),
                    refund_total=refunded_total,
                    metadata_extra={
                        "charge_id": charge_id,
                        "refund_amount": refund_amount,
                        "source_event": "charge.refunded",
                    },
                )
                logger.info(f"Order {order_id} refunded: {refund_amount}")
        elif event_type == "refund.created":
            refund_id = data.get("id")
            refund_status = str(data.get("status") or "").strip().lower()
            payment_intent_id = data.get("payment_intent")
            refund_amount = data.get("amount")
            currency = (data.get("currency") or "").strip().lower() or None
            refund_meta = _stripe_object_to_dict(data.get("metadata") or {})
            refund_snapshot = extract_stripe_refund_snapshot(
                data,
                source_event="refund.created",
            )

            result = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=refund_meta if isinstance(refund_meta, dict) else None,
            )

            if result:
                await _persist_stripe_refund_observability(result, refund_snapshot)
                await log_order_event(
                    event_type="refund_created_webhook",
                    order_id=result["order_id"],
                    merchant_id=result["merchant_id"],
                    metadata={
                        "refund_id": refund_id,
                        "payment_intent_id": payment_intent_id,
                        "refund_amount": refund_amount,
                        "currency": currency or str(result.get("currency") or ""),
                        "status": refund_status or "unknown",
                        "pending_reason": refund_snapshot.get("pending_reason"),
                        "reference_status": refund_snapshot.get("reference_status"),
                        "reference_type": refund_snapshot.get("reference_type"),
                        "reference": refund_snapshot.get("reference"),
                    },
                )

        elif event_type == "refund.updated":
            refund_id = data.get("id")
            refund_status = str(data.get("status") or "").strip().lower()
            payment_intent_id = data.get("payment_intent")
            refund_amount = data.get("amount")
            currency = (data.get("currency") or "").strip().lower() or None
            refund_meta = _stripe_object_to_dict(data.get("metadata") or {})
            pending_reason = data.get("pending_reason")
            refund_snapshot = extract_stripe_refund_snapshot(
                data,
                source_event="refund.updated",
            )

            result = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=refund_meta if isinstance(refund_meta, dict) else None,
            )

            if result:
                order_id = result["order_id"]
                existing_meta = result.get("metadata") or {}
                if not isinstance(existing_meta, dict):
                    existing_meta = {}

                if refund_status == "succeeded":
                    try:
                        refunded_minor = Decimal(str(refund_amount)) if refund_amount is not None else Decimal("0")
                    except Exception:
                        refunded_minor = Decimal("0")
                    factor = _stripe_minor_unit_factor(currency or str(result.get("currency") or ""))
                    try:
                        refunded_total = refunded_minor / factor
                    except Exception:
                        refunded_total = Decimal("0")
                    await _finalize_stripe_refund_success(
                        result,
                        refund_reference=refund_id,
                        refund_amount_minor=refund_amount,
                        currency=currency or str(result.get("currency") or ""),
                        refund_total=refunded_total,
                        metadata_extra={
                            "refund_id": refund_id,
                            "refund_amount": refund_amount,
                            "status": refund_status,
                            "source_event": "refund.updated",
                            **refund_snapshot,
                        },
                        metadata_patch={
                            **stripe_refund_metadata_patch(
                                refund_snapshot,
                                existing_metadata=existing_meta,
                            ),
                            "stripe_refund_updated": {
                                "refund_id": refund_id,
                                "amount_minor": refund_amount,
                                "currency": currency or str(result.get("currency") or ""),
                                "status": refund_status,
                                "pending_reason": refund_snapshot.get("pending_reason"),
                                "reference": refund_snapshot.get("reference"),
                                "reference_status": refund_snapshot.get("reference_status"),
                                "reference_type": refund_snapshot.get("reference_type"),
                                "tracking_reference_kind": refund_snapshot.get("tracking_reference_kind"),
                                "destination_type": refund_snapshot.get("destination_type"),
                                "is_reversal": refund_snapshot.get("is_reversal"),
                                "received_at": datetime.now().isoformat(),
                            }
                        },
                    )
                elif refund_status == "failed":
                    failure_reason = data.get("failure_reason") or refund_status or "unknown"
                    await _finalize_stripe_refund_failure(
                        result,
                        refund_reference=refund_id,
                        refund_amount_minor=refund_amount,
                        currency=currency or str(result.get("currency") or ""),
                        failure_reason=failure_reason,
                        refund_snapshot=refund_snapshot,
                        metadata_extra={"source_event": "refund.updated"},
                    )
                else:
                    await _persist_stripe_refund_observability(result, refund_snapshot)
                    await log_order_event(
                        event_type="refund_pending_webhook" if refund_status == "pending" else "refund_updated_webhook",
                        order_id=order_id,
                        merchant_id=result["merchant_id"],
                        metadata={
                            "refund_id": refund_id,
                            "payment_intent_id": payment_intent_id,
                            "refund_amount": refund_amount,
                            "status": refund_status or "unknown",
                            "pending_reason": pending_reason,
                            "reference": refund_snapshot.get("reference"),
                            "reference_status": refund_snapshot.get("reference_status"),
                            "reference_type": refund_snapshot.get("reference_type"),
                            "tracking_reference_kind": refund_snapshot.get("tracking_reference_kind"),
                            "destination_type": refund_snapshot.get("destination_type"),
                            "is_reversal": refund_snapshot.get("is_reversal"),
                        },
                    )

        elif event_type == "refund.failed":
            refund_id = data.get("id")
            payment_intent_id = data.get("payment_intent")
            refund_amount = data.get("amount")
            currency = (data.get("currency") or "").strip().lower() or None
            failure_reason = data.get("failure_reason") or data.get("status") or "unknown"
            refund_snapshot = extract_stripe_refund_snapshot(
                data,
                source_event="refund.failed",
            )

            refund_meta = data.get("metadata") or {}
            result = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=refund_meta if isinstance(refund_meta, dict) else None,
            )

            if result:
                finalization = await _finalize_stripe_refund_failure(
                    result,
                    refund_reference=refund_id,
                    refund_amount_minor=refund_amount,
                    currency=currency or str(result.get("currency") or ""),
                    failure_reason=failure_reason,
                    refund_snapshot=refund_snapshot,
                )
                logger.warning(
                    "Stripe refund failed for order %s refund_id=%s rollback_applied=%s",
                    result["order_id"],
                    refund_id,
                    finalization.get("rolled_back"),
                )

        elif event_type and str(event_type).startswith("charge.dispute."):
            # Stripe dispute/chargeback signals.
            # Do not mutate order state here; treat as risk/ops signal and persist best-effort.
            try:
                from services.dispute_records_service import (
                    stripe_dispute_status_detail,
                    upsert_stripe_dispute_record_best_effort,
                )
                dispute_payload = {}
                if isinstance(data, dict):
                    dispute_payload = data
                elif hasattr(data, "to_dict"):
                    try:
                        dispute_payload = data.to_dict()
                    except Exception:
                        dispute_payload = {}

                await upsert_stripe_dispute_record_best_effort(
                    dispute_payload,
                    event_type=str(event_type),
                )
            except Exception:
                pass
            try:
                from services.pcs_evidence_pack_service import create_dispute_evidence_pack

                dispute_meta = {}
                if isinstance(dispute_payload, dict):
                    raw_meta = dispute_payload.get("metadata") or {}
                    dispute_meta = raw_meta if isinstance(raw_meta, dict) else {}
                merchant_id = str(dispute_meta.get("merchant_id") or "").strip()
                order_id = str(dispute_meta.get("order_id") or "").strip() or None
                dispute_ref = str((dispute_payload or {}).get("id") or "").strip()
                raw_status = str((dispute_payload or {}).get("status") or "").strip().lower()
                dispute_status_detail = stripe_dispute_status_detail(raw=raw_status or None, event_type=event_type)
                if merchant_id and dispute_ref:
                    await create_dispute_evidence_pack(
                        merchant_id=merchant_id,
                        dispute_ref=dispute_ref,
                        order_id=order_id,
                        dispute_payload=dict(dispute_payload or {}),
                        source="stripe",
                        status=str(dispute_status_detail["pack_status"]),
                        event_type=str(event_type or "") or None,
                        triggered_by=f"stripe_webhook:{event_type}",
                    )
            except Exception:
                pass
        
        return {"status": "success", "event": event_type}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error handling Stripe webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Shopify GDPR Webhooks (static endpoint for Partner Dashboard configuration)
# ============================================================================

@router.post("/shopify/gdpr")
async def handle_shopify_gdpr_webhook(
    request: Request,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None),
    x_shopify_shop_domain: Optional[str] = Header(None),
):
    """
    Shopify data privacy webhooks:
    - customers/data_request
    - customers/redact
    - shop/redact
    Must be configured in Shopify Partner Dashboard with a static URL.
    """
    payload = await request.body()
    topic = x_shopify_topic or "unknown"
    shop_domain = _canonicalize_shop_domain(x_shopify_shop_domain)

    app_secret = (settings.shopify_client_secret or "").strip()
    if not app_secret:
        record_shopify_webhook(result="error", reason="missing_secret", topic=topic)
        raise HTTPException(status_code=500, detail="Shopify webhook verification not configured")
    if not x_shopify_hmac_sha256:
        record_shopify_webhook(result="error", reason="missing_hmac", topic=topic)
        raise HTTPException(status_code=401, detail="Missing Shopify webhook signature")
    if not verify_shopify_hmac(secret=app_secret, payload=payload, header_hmac_base64=x_shopify_hmac_sha256):
        record_shopify_webhook(result="error", reason="invalid_signature", topic=topic)
        raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

    merchant_id: Optional[str] = None
    if shop_domain:
        try:
            from db.database import database

            row = await database.fetch_one(
                """
                SELECT merchant_id
                FROM merchant_stores
                WHERE platform = 'shopify' AND lower(domain) = :domain
                ORDER BY connected_at DESC
                LIMIT 1
                """,
                {"domain": shop_domain},
            )
            if row:
                merchant_id = row["merchant_id"]
            else:
                row = await database.fetch_one(
                    """
                    SELECT merchant_id
                    FROM merchant_onboarding
                    WHERE lower(mcp_shop_domain) = :domain
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    {"domain": shop_domain},
                )
                if row:
                    merchant_id = row["merchant_id"]
        except Exception as e:
            logger.warning("GDPR webhook merchant lookup failed shop=%s err=%s", shop_domain, str(e)[:200])

    payload_keys: list[str] = []
    try:
        data = json.loads(payload.decode("utf-8"))
        if isinstance(data, dict):
            payload_keys = list(data.keys())
    except Exception:
        payload_keys = []

    if merchant_id:
        try:
            await log_order_event(
                event_type="gdpr_webhook",
                order_id=f"gdpr_{merchant_id}",
                merchant_id=merchant_id,
                metadata={"topic": topic, "payload_keys": payload_keys, "shop_domain": shop_domain},
            )
        except Exception as e:
            logger.warning("GDPR webhook log failed merchant=%s err=%s", merchant_id, str(e)[:200])

    record_shopify_webhook(result="success", reason="ok", topic=topic)
    return {"status": "success", "topic": topic}


# ============================================================================
# Shopify Webhooks
# ============================================================================

@router.post("/shopify/{merchant_id}")
async def handle_shopify_webhook(
    merchant_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None),
    x_shopify_shop_domain: Optional[str] = Header(None),
    x_shopify_webhook_id: Optional[str] = Header(None),
    x_shopify_triggered_at: Optional[str] = Header(None),
):
    """
    处理 Shopify 事件
    
	    支持的事件：
	    - orders/create, orders/updated, orders/paid, orders/cancelled
	    - fulfillments/create, fulfillments/update, orders/fulfilled (legacy)
	    - refunds/create (preferred) / orders/refunded (legacy)
	    - returns/* (Shopify Returns; topic availability varies by shop/app)
	    """
    try:
        payload = await request.body()
        topic = x_shopify_topic or "unknown"
        
        # 获取商户信息
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        got_canon = _canonicalize_shop_domain(x_shopify_shop_domain)

        # Build Shopify store allowlist and (optional) per-store webhook secret mapping.
        # Note: the shop domain comes from an untrusted header, so we ONLY use it to select
        # a secret after confirming it matches a connected Shopify store for this merchant.
        stores = []
        try:
            stores = await get_merchant_active_stores(merchant_id)
        except Exception as e:
            logger.warning("Shopify webhook store lookup failed merchant=%s err=%s", merchant_id, str(e)[:160])
            stores = []

        allowed_domains: Dict[str, Dict[str, Any]] = {}
        for store in stores or []:
            if (store.get("platform") or "").lower() != "shopify":
                continue
            dom = _canonicalize_shop_domain(store.get("domain"))
            if dom:
                allowed_domains[dom] = store

        matched_store = allowed_domains.get(got_canon) if got_canon else None
        store_secret: str = ""
        if matched_store:
            creds = matched_store.get("api_credentials") or {}
            if isinstance(creds, dict):
                for k in ("webhook_secret", "client_secret", "api_secret_key", "shopify_client_secret"):
                    v = creds.get(k)
                    if isinstance(v, str) and v.strip():
                        store_secret = v.strip()
                        break

        # Verify signature (strict in production; must use raw request body).
        instance_id = socket.gethostname()
        is_production = (
            os.getenv("APP_ENV", "").lower() == "production"
            or os.getenv("ENVIRONMENT", "").lower() == "production"
            or bool(os.getenv("RAILWAY_GIT_COMMIT_SHA"))
        )
        app_secret = getattr(settings, "shopify_client_secret", None)
        app_secret = app_secret.strip() if isinstance(app_secret, str) else ""
        store_secret = store_secret.strip() if isinstance(store_secret, str) else ""

        # Webhook signatures are generated using the Shopify app's shared secret. In practice, we may
        # have BOTH an app-level secret (env) and a per-store override (DB) during migrations or when
        # merchants reconnect via different onboarding flows. Accept when ANY known secret matches.
        secret_candidates: list[tuple[str, str]] = []
        if store_secret:
            secret_candidates.append(("store_credentials", store_secret))
        if app_secret and app_secret != store_secret:
            secret_candidates.append(("app_env", app_secret))

        secret_source = secret_candidates[0][0] if secret_candidates else "none"
        secret_len = len(secret_candidates[0][1]) if secret_candidates else 0
        has_store_secret = bool(store_secret)
        has_app_secret = bool(app_secret)
        store_secret_len = len(store_secret) if store_secret else 0
        app_secret_len = len(app_secret) if app_secret else 0

        debug_meta = {
            "merchant_id": merchant_id,
            "instance": instance_id,
            "topic": topic,
            "webhook_id": x_shopify_webhook_id,
            "shop_domain": got_canon,
            "has_shop_domain_header": bool(x_shopify_shop_domain),
            "has_hmac_header": bool(x_shopify_hmac_sha256),
            "has_webhook_id_header": bool(x_shopify_webhook_id),
            "content_length": request.headers.get("content-length"),
            "user_agent": request.headers.get("user-agent"),
            "secret_source": secret_source,
            "secret_len": secret_len,
            "has_store_secret": has_store_secret,
            "has_app_secret": has_app_secret,
            "store_secret_len": store_secret_len,
            "app_secret_len": app_secret_len,
            "secret_candidate_count": len(secret_candidates),
        }
        if is_production:
            if not x_shopify_shop_domain:
                record_shopify_webhook(result="error", reason="missing_shop_domain", topic=topic)
                logger.warning("Shopify webhook rejected: missing shop domain header %s", debug_meta)
                raise HTTPException(status_code=401, detail="Missing Shopify shop domain")
            if not allowed_domains:
                record_shopify_webhook(result="error", reason="no_shopify_store", topic=topic)
                logger.error(
                    "Shopify webhook rejected: no Shopify store configured merchant=%s topic=%s got=%s",
                    merchant_id,
                    topic,
                    got_canon,
                )
                raise HTTPException(status_code=400, detail="No Shopify store connected")
            if got_canon and got_canon not in allowed_domains:
                record_shopify_webhook(result="error", reason="shop_domain_mismatch", topic=topic)
                logger.error(
                    "Shopify webhook shop_domain mismatch merchant=%s allowed=%s got=%s topic=%s",
                    merchant_id,
                    sorted(list(allowed_domains.keys())),
                    got_canon,
                    topic,
                )
                raise HTTPException(status_code=403, detail="Shop domain mismatch")
            if not secret_candidates:
                record_shopify_webhook(result="error", reason="missing_secret", topic=topic)
                logger.error(
                    "Shopify webhook verification secret missing merchant=%s topic=%s source=%s",
                    merchant_id,
                    topic,
                    secret_source,
                )
                raise HTTPException(status_code=500, detail="Shopify webhook verification not configured")
            if not x_shopify_hmac_sha256:
                record_shopify_webhook(result="error", reason="missing_hmac", topic=topic)
                logger.warning("Shopify webhook rejected: missing HMAC header %s", debug_meta)
                raise HTTPException(status_code=401, detail="Missing Shopify webhook signature")

        signature_verified = False
        verified_source = None
        for source, secret in secret_candidates:
            if verify_shopify_hmac(secret=secret, payload=payload, header_hmac_base64=x_shopify_hmac_sha256):
                signature_verified = True
                verified_source = source
                break
        debug_meta["verified_secret_source"] = verified_source
        if is_production and not signature_verified:
            record_shopify_webhook(result="error", reason="invalid_signature", topic=topic)
            # This commonly indicates env drift across instances (different SHOPIFY_CLIENT_SECRET).
            meta = dict(debug_meta)
            if x_shopify_hmac_sha256:
                meta["hmac_prefix"] = x_shopify_hmac_sha256[:10]
            logger.warning("Shopify webhook rejected: invalid signature %s", meta)
            raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

        # Parse event
        data = json.loads(payload)
        shop_domain = x_shopify_shop_domain or merchant.get("mcp_shop_domain") or "unknown"

        # Occurred_at is best-effort; Shopify may provide X-Shopify-Triggered-At.
        occurred_at: Optional[datetime] = None
        if x_shopify_triggered_at:
            try:
                occurred_at = datetime.fromisoformat(x_shopify_triggered_at.replace("Z", "+00:00"))
            except Exception:
                occurred_at = None

        # Persist event (append-only) with idempotency guard
        try:
            is_dup, _row = await ingest_shopify_webhook(
                merchant_id=merchant_id,
                topic=topic,
                payload=payload,
                shop_domain=shop_domain,
                webhook_id=x_shopify_webhook_id,
                occurred_at=occurred_at,
                signature_verified=signature_verified,
            )
            if is_dup:
                record_shopify_webhook(result="success", reason="duplicate", topic=topic)
                return {"status": "success", "topic": topic, "duplicate": True}
        except Exception as e:
            # In production, fail so Shopify will retry and we don't lose the audit trail.
            logger.warning(f"PCS webhook event persistence failed merchant={merchant_id} topic={topic}: {e}")
            if is_production:
                record_shopify_webhook(result="error", reason="persist_failed", topic=topic)
                raise HTTPException(status_code=500, detail="Webhook event persistence unavailable")

        record_shopify_webhook(result="success", reason="ok", topic=topic)
        logger.info(f"Received Shopify webhook for {merchant_id}: {topic}")

        if topic.startswith("products/"):
            catalog_event = await record_catalog_sync_event(
                merchant_id=merchant_id,
                connector="shopify",
                event_type="product_webhook",
                topic=topic,
                payload_json=data if isinstance(data, dict) else {"raw": data},
                source_ref=x_shopify_webhook_id or f"{merchant_id}:{topic}",
                occurred_at=occurred_at,
            )
            catalog_job = await create_catalog_sync_job(
                merchant_id=merchant_id,
                connector="shopify",
                mode="webhook",
                scope={
                    "platform": "shopify",
                    "limit": 5000,
                    "include_expired": True,
                    "source_system": "products_cache",
                    "source_ref": x_shopify_webhook_id or f"{merchant_id}:{topic}",
                    "trigger_topic": topic,
                },
                requested_by="shopify_webhook",
            )
            background_tasks.add_task(
                _run_shopify_catalog_webhook_reconcile,
                merchant_id=merchant_id,
                job_id=str(catalog_job.get("job_id") or ""),
                event_id=str(catalog_event.get("event_id") or ""),
                limit=5000,
            )

        if topic == "app/uninstalled":
            try:
                from db.database import database

                canon_domain = _canonicalize_shop_domain(shop_domain)
                if canon_domain:
                    await database.execute(
                        """
                        UPDATE merchant_stores
                        SET status = 'disconnected',
                            api_key = NULL,
                            last_sync = NOW()
                        WHERE merchant_id = :merchant_id
                          AND platform = 'shopify'
                          AND lower(domain) = :domain
                        """,
                        {"merchant_id": merchant_id, "domain": canon_domain},
                    )
                await database.execute(
                    """
                    UPDATE merchant_onboarding
                    SET mcp_connected = FALSE,
                        mcp_access_token = NULL
                    WHERE merchant_id = :merchant_id
                    """,
                    {"merchant_id": merchant_id},
                )
                await log_order_event(
                    event_type="shopify_app_uninstalled",
                    order_id=f"shopify_app_uninstalled_{merchant_id}",
                    merchant_id=merchant_id,
                    metadata={"shop_domain": canon_domain or shop_domain},
                )
            except Exception as e:
                logger.warning("Shopify app uninstall cleanup failed merchant=%s err=%s", merchant_id, str(e)[:200])
            return {"status": "success", "topic": topic}
        if topic in ("orders/fulfilled", "fulfillments/create", "fulfillments/update"):
            # 履约更新（订单级 or fulfillment 级）
            tracking_numbers = []
            carrier: Optional[str] = None

            # fulfillments/* 通常是 fulfillment object，包含 order_id + tracking_numbers
            if topic.startswith("fulfillments/") and data.get("order_id"):
                shopify_order_id = str(data.get("order_id"))
                if isinstance(data.get("tracking_numbers"), list):
                    tracking_numbers.extend([str(x) for x in data.get("tracking_numbers") if x])
                if data.get("tracking_number"):
                    tracking_numbers.append(str(data.get("tracking_number")))
                carrier = data.get("tracking_company") or data.get("tracking_company_name") or data.get("carrier")
            else:
                # orders/fulfilled 通常是 order object，包含 fulfillments[]
                shopify_order_id = str(data.get("id"))
                for fulfillment in data.get("fulfillments", []) or []:
                    if isinstance(fulfillment, dict):
                        if not carrier:
                            carrier = fulfillment.get("tracking_company") or fulfillment.get("tracking_company_name")
                        if isinstance(fulfillment.get("tracking_numbers"), list):
                            tracking_numbers.extend([str(x) for x in (fulfillment.get("tracking_numbers") or []) if x])
                        if fulfillment.get("tracking_number"):
                            tracking_numbers.append(str(fulfillment.get("tracking_number")))
            # 更新 Pivota 订单
            query = "SELECT * FROM orders WHERE shopify_order_id = :shopify_order_id"
            from db.database import database
            result = await database.fetch_one(query, {"shopify_order_id": shopify_order_id})

            if result:
                order_id = result["order_id"]
                tracking_number = ", ".join(dict.fromkeys(tracking_numbers)) if tracking_numbers else None

                await mark_order_shipped(order_id, tracking_number, carrier=carrier)
                await log_order_event(
                    event_type="fulfillment_webhook",
                    order_id=order_id,
                    merchant_id=merchant_id,
                    metadata={
                        "shopify_order_id": shopify_order_id,
                        "tracking_numbers": tracking_numbers,
                        "carrier": carrier,
                    }
                )
                logger.info(f"Order {order_id} marked as shipped via webhook")

                if _reviews_invitation_auto_send_on_shopify_fulfillment_enabled():
                    async def send_review_invitation_task() -> None:
                        try:
                            internal_key = (_reviews_invitation_internal_key() or "").strip()
                            if not internal_key:
                                logger.info("Reviews invitation issuer disabled; skip send.")
                                return
                            if _reviews_invitation_send_delay_seconds() > 0:
                                ok = await enqueue_invitation_email_send_job_from_order(
                                    merchant_id=merchant_id,
                                    order_id=order_id,
                                )
                                logger.info(f"Reviews invitation job enqueued for order {order_id} ok={ok}")
                                return
                            req = SendInvitationEmailFromOrderRequest(
                                merchant_id=merchant_id,
                                order_id=order_id,
                                ttl_seconds=7 * 24 * 3600,
                            )
                            await send_invitation_email_from_order(
                                body=req,
                                response=Response(),
                                x_internal_key=internal_key,
                            )
                            logger.info(f"Reviews invitation email dispatched for order {order_id} (shopify webhook)")
                        except HTTPException as e:
                            logger.warning(
                                f"Reviews invitation skipped for order {order_id} (shopify webhook): {e.detail}"
                            )
                        except Exception as e:
                            logger.error(
                                f"Reviews invitation error for order {order_id} (shopify webhook): {e}"
                            )

                    background_tasks.add_task(send_review_invitation_task)

        elif topic in ("orders/create", "orders/paid"):
            # Best-effort linkage: map Shopify order id -> Pivota order by orders.shopify_order_id
            shopify_order_id = str(data.get("id"))
            from db.database import database
            result = await database.fetch_one(
                "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                {"shopify_order_id": shopify_order_id},
            )
            pivota_order_id = result["order_id"] if result else f"shopify_{shopify_order_id}"
            await log_order_event(
                event_type="shopify_order_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={"shopify_order_id": shopify_order_id, "topic": topic},
            )

        elif topic == "orders/cancelled":
            # 订单取消
            shopify_order_id = str(data.get("id"))
            cancel_reason = data.get("cancel_reason")

            query = "SELECT * FROM orders WHERE shopify_order_id = :shopify_order_id"
            from db.database import database
            result = await database.fetch_one(query, {"shopify_order_id": shopify_order_id})

            if result:
                order_id = result["order_id"]
                await update_order_status(order_id, "cancelled")
                await log_order_event(
                    event_type="order_cancelled_webhook",
                    order_id=order_id,
                    merchant_id=merchant_id,
                    metadata={
                        "shopify_order_id": shopify_order_id,
                        "cancel_reason": cancel_reason
                    }
                )
                logger.info(f"Order {order_id} cancelled via webhook: {cancel_reason}")

        elif topic == "orders/updated":
            # 订单更新
            shopify_order_id = str(data.get("id"))
            financial_status = data.get("financial_status")
            fulfillment_status = data.get("fulfillment_status")
            from db.database import database

            pivota = await database.fetch_one(
                "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                {"shopify_order_id": shopify_order_id},
            )
            pivota_order_id = pivota["order_id"] if pivota else f"shopify_{shopify_order_id}"

            await log_order_event(
                event_type="order_updated_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={
                    "shopify_order_id": shopify_order_id,
                    "financial_status": financial_status,
                    "fulfillment_status": fulfillment_status
                }
            )
            logger.info(f"Shopify order {shopify_order_id} updated")

            # Compatibility: some shops/apps only reliably emit orders/updated when fulfillment changes.
            # If Shopify indicates fulfilled here, converge Pivota order state to shipped.
            try:
                phase = "start"
                raw_fulfillment_status = str(fulfillment_status or "").strip().lower()
                if raw_fulfillment_status == "fulfilled" and pivota:
                    phase = "load_current"
                    current = await database.fetch_one(
                        "SELECT order_id, fulfillment_status, tracking_number FROM orders WHERE shopify_order_id = :shopify_order_id",
                        {"shopify_order_id": shopify_order_id},
                    )
                    if current:
                        phase = "check_current_status"
                        current_status = str(current["fulfillment_status"] if "fulfillment_status" in current else "").strip().lower()
                        if current_status not in {"shipped", "delivered"}:
                            phase = "extract_tracking"
                            tracking_numbers: list[str] = []
                            carrier: Optional[str] = None
                            for fulfillment in (data.get("fulfillments") or []) or []:
                                if isinstance(fulfillment, dict):
                                    if not carrier:
                                        carrier = fulfillment.get("tracking_company") or fulfillment.get("tracking_company_name")
                                    if isinstance(fulfillment.get("tracking_numbers"), list):
                                        tracking_numbers.extend(
                                            [str(x) for x in (fulfillment.get("tracking_numbers") or []) if x]
                                        )
                                    if fulfillment.get("tracking_number"):
                                        tracking_numbers.append(str(fulfillment.get("tracking_number")))
                                    if fulfillment.get("tracking_info") and isinstance(fulfillment.get("tracking_info"), dict):
                                        ti = fulfillment.get("tracking_info") or {}
                                        if not carrier:
                                            carrier = ti.get("company") or ti.get("tracking_company") or ti.get("carrier")
                                        if ti.get("number"):
                                            tracking_numbers.append(str(ti.get("number")))
                            phase = "mark_order_shipped"
                            tracking_number = ", ".join(dict.fromkeys(tracking_numbers)) if tracking_numbers else None
                            shipped_ok = await mark_order_shipped(
                                str(current["order_id"]),
                                tracking_number,
                                carrier=carrier,
                            )
                            if not shipped_ok:
                                phase = "mark_order_shipped_returned_false"
                                await log_order_event(
                                    event_type="shopify_fulfillment_convergence_failed",
                                    order_id=str(current["order_id"]),
                                    merchant_id=merchant_id,
                                    metadata={
                                        "shopify_order_id": shopify_order_id,
                                        "topic": topic,
                                        "fulfillment_status": fulfillment_status,
                                        "reason": "mark_order_shipped_returned_false",
                                    },
                                )
                                logger.warning(
                                    "orders/updated fulfillment convergence failed: mark_order_shipped returned false shopify_order_id=%s order_id=%s",
                                    shopify_order_id,
                                    str(current["order_id"]),
                                )
                                return {"status": "success", "topic": topic}
                            phase = "log_success_event"
                            await log_order_event(
                                event_type="fulfillment_via_order_updated_webhook",
                                order_id=str(current["order_id"]),
                                merchant_id=merchant_id,
                                metadata={
                                    "shopify_order_id": shopify_order_id,
                                    "tracking_numbers": tracking_numbers,
                                    "carrier": carrier,
                                },
                            )
                            logger.info(f"Order {current['order_id']} marked as shipped via orders/updated webhook")
            except Exception as e:
                # Persist a minimal error breadcrumb into the immutable order events table so ops can debug
                # without relying on Railway logs. Keep it short and non-PII.
                try:
                    await log_order_event(
                        event_type="shopify_fulfillment_convergence_error",
                        order_id=pivota_order_id,
                        merchant_id=merchant_id,
                        metadata={
                            "shopify_order_id": shopify_order_id,
                            "topic": topic,
                            "fulfillment_status": fulfillment_status,
                            "phase": locals().get("phase", "unknown"),
                            "error_type": type(e).__name__,
                            "error": str(e)[:200],
                        },
                    )
                except Exception:
                    pass
                logger.exception(
                    "orders/updated fulfillment convergence error shopify_order_id=%s",
                    shopify_order_id,
                )

        elif topic in ("refunds/create", "orders/refunded"):
            platform_order_id = str(data.get("order_id") or data.get("id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = (
                pivota["order_id"]
                if pivota
                else (f"shopify_{platform_order_id}" if platform_order_id else f"shopify_refund_{datetime.utcnow().timestamp()}")
            )

            await log_order_event(
                event_type="refund_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={"topic": topic, "shopify_order_id": platform_order_id or None},
            )

            # Best-effort normalize using existing adapter.
            try:
                from routes.refund_webhook_routes import process_platform_refund
                from services.platform_refund_adapter import platform_refund_adapter

                refund_event = platform_refund_adapter.normalize_refund_event("shopify", data)
                result = await process_platform_refund(refund_event, merchant_id)
                logger.info(f"Processed Shopify refund webhook for merchant {merchant_id}: {result.get('status')}")
            except Exception as e:
                logger.warning(f"Failed to process Shopify refund webhook merchant={merchant_id}: {e}")

        elif topic == "tender_transactions/create":
            # Money movement signal (payment/refund). Best-effort: record as immutable event; do not assume state transitions.
            platform_order_id = str(data.get("order_id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = pivota["order_id"] if pivota else (f"shopify_{platform_order_id}" if platform_order_id else f"shopify_tender_{datetime.utcnow().timestamp()}")
            await log_order_event(
                event_type="tender_transaction_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "shopify_order_id": platform_order_id or None,
                    "kind": data.get("kind"),
                    "amount": data.get("amount"),
                    "currency": data.get("currency"),
                    "status": data.get("status"),
                    "tender_transaction_id": data.get("id"),
                },
            )

        elif topic in ("disputes/create", "disputes/update"):
            # Dispute signals are critical for tiering/risk; store event and best-effort link to order_id.
            platform_order_id = str(data.get("order_id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = pivota["order_id"] if pivota else (f"shopify_{platform_order_id}" if platform_order_id else "shopify_dispute_unknown")
            await log_order_event(
                event_type="dispute_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "shopify_order_id": platform_order_id or None,
                    "dispute_id": data.get("id"),
                    "status": data.get("status"),
                    "reason": data.get("reason"),
                    "amount": data.get("amount"),
                    "currency": data.get("currency"),
                },
            )

            # MVP measurement scaffolding: dispute opened/resolved (metadata-only).
            try:
                from mvp.constants import EVENT_DISPUTE_OPENED, EVENT_DISPUTE_RESOLVED, SURFACE_BACKEND
                from mvp.events import emit_best_effort

                raw_status = (data.get("status") or "").lower()
                is_resolved = raw_status in {"won", "lost", "resolved", "closed"}
                evt = EVENT_DISPUTE_RESOLVED if is_resolved else EVENT_DISPUTE_OPENED

                emit_best_effort(
                    event_type=evt,
                    payload={
                        "merchant_id": merchant_id,
                        "order_id": pivota_order_id,
                        "shopify_order_id": platform_order_id or None,
                        "dispute_id": data.get("id"),
                        "status": data.get("status"),
                        "reason": data.get("reason"),
                        "amount": data.get("amount"),
                        "currency": data.get("currency"),
                    },
                    merchant_id=merchant_id,
                    geo=None,
                    surface=SURFACE_BACKEND,
                    adapter="shopify_webhook",
                    risk_tier="unknown",
                    idempotency_key=str(data.get("id") or "") or None,
                )
            except Exception:
                pass

            # PCS: best-effort dispute evidence pack builder (draft on open, frozen on resolution).
            try:
                from services.pcs_evidence_pack_service import create_dispute_evidence_pack

                raw_status = (data.get("status") or "").lower()
                is_resolved = raw_status in {"won", "lost", "resolved", "closed"}
                await create_dispute_evidence_pack(
                    merchant_id=str(merchant_id),
                    dispute_ref=str(data.get("id") or ""),
                    order_id=str(pivota_order_id) if pivota_order_id else None,
                    dispute_payload=dict(data or {}),
                    source="shopify",
                    status="frozen" if is_resolved else "draft",
                    triggered_by=f"shopify_webhook:{topic}",
                )
            except Exception:
                pass

            # MVP ledger event (best-effort): dispute timeline entry.
            try:
                from mvp.ledger_events import emit_ledger_event_best_effort

                raw_status = (data.get("status") or "").lower()
                is_resolved = raw_status in {"won", "lost", "resolved", "closed"}
                emit_ledger_event_best_effort(
                    merchant_id=str(merchant_id),
                    event_type="dispute_resolved" if is_resolved else "dispute_opened",
                    order_id=str(pivota_order_id) if pivota_order_id else None,
                    source={"type": "shopify_webhook", "external_event_id": str(data.get("id") or "")},
                    amount={
                        "value": float(data.get("amount") or 0.0),
                        "currency": str(data.get("currency") or "USD"),
                    }
                    if (data.get("amount") is not None)
                    else None,
                    refs={"shopify_order_id": platform_order_id or None},
                    geo=None,
                    surface="backend",
                    adapter="shopify_webhook",
                    risk_tier="unknown",
                    idempotency_key=str(data.get("id") or "") or None,
                    signature_verified=True,
                )
            except Exception:
                pass

            # Upsert normalized dispute record for ops visibility (best-effort).
            try:
                from services.dispute_records_service import upsert_shopify_dispute_record_best_effort

                await upsert_shopify_dispute_record_best_effort(
                    merchant_id=str(merchant_id),
                    payload=dict(data or {}),
                    topic=str(topic),
                )
            except Exception:
                pass

        elif topic and topic.startswith("returns/"):
            # Returns/RMA signals (if enabled). Best-effort: record and upsert minimal return record.
            platform_order_id = str(data.get("order_id") or data.get("shopify_order_id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = (
                pivota["order_id"]
                if pivota
                else (f"shopify_{platform_order_id}" if platform_order_id else "shopify_return_unknown")
            )

            await log_order_event(
                event_type="return_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={"topic": topic, "shopify_order_id": platform_order_id or None},
            )

            try:
                from services.return_records_service import upsert_shopify_return_record_best_effort

                await upsert_shopify_return_record_best_effort(
                    merchant_id=str(merchant_id),
                    payload=dict(data or {}),
                    topic=str(topic),
                )
            except Exception:
                pass

        elif topic in ("customers/data_request", "customers/redact", "shop/redact"):
            await log_order_event(
                event_type="gdpr_webhook",
                order_id=f"gdpr_{merchant_id}",
                merchant_id=merchant_id,
                metadata={"topic": topic, "payload_keys": list((data or {}).keys())},
            )
        
        return {"status": "success", "topic": topic}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error handling Shopify webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Webhook 注册（设置 Shopify webhooks）
# ============================================================================

@router.post("/register/shopify/{merchant_id}")
async def register_shopify_webhooks(
    merchant_id: str,
    callback_base_url: str
):
    """
    为商户注册 Shopify webhooks
    
    Args:
        merchant_id: 商户 ID
        callback_base_url: Webhook 回调的基础 URL（如 https://api.pivota.com）
    """
    try:
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        stores = await get_merchant_active_stores(merchant_id)
        shopify_store = None
        for store in stores or []:
            if (store.get("platform") or "").lower() != "shopify":
                continue
            if store.get("domain") and store.get("api_key"):
                shopify_store = store
                break
        if not shopify_store:
            raise HTTPException(status_code=400, detail="No Shopify store connected")

        shop_domain = shopify_store.get("domain")
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=shopify_store.get("api_key_raw") or shopify_store.get("api_key"),
            store_id=str(shopify_store.get("store_id") or "").strip() or None,
        )
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Missing Shopify credentials")

        shop_domain_canon = _canonicalize_shop_domain(shop_domain)
        if not shop_domain_canon:
            raise HTTPException(status_code=400, detail="Invalid Shopify store domain")
        
        # 要注册的 webhook topics
        topics = [
            # Orders
            "orders/create",
            "orders/updated",
            "orders/paid",
            "orders/cancelled",
            # Fulfillments
            "fulfillments/create",
            "fulfillments/update",
            # Legacy support
            "orders/fulfilled",
            # Uninstall
            "app/uninstalled",
            # Refunds (preferred)
            "refunds/create",
            # Money movement (refund funds settled / payment settled signals)
            "tender_transactions/create",
            # Disputes (Shopify Payments)
            "disputes/create",
            "disputes/update",
            # Returns (if enabled by shop/app)
            "returns/create",
            "returns/update",
            # GDPR compliance (required when accessing customer/order data)
            "customers/data_request",
            "customers/redact",
            "shop/redact",
        ]
        
        registered = []
        already_exists = []
        failed = []
        import httpx
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            for topic in topics:
                webhook_data = {
                    "webhook": {
                        "topic": topic,
                        "address": f"{callback_base_url.rstrip('/')}/webhooks/shopify/{merchant_id}",
                        "format": "json"
                    }
                }
                
                url = f"https://{shop_domain_canon}/admin/api/2024-07/webhooks.json"
                headers = {
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json"
                }
                
                response = await client.post(url, json=webhook_data, headers=headers)
                
                if response.status_code == 201:
                    webhook = response.json()["webhook"]
                    registered.append({
                        "topic": topic,
                        "webhook_id": webhook["id"]
                    })
                    logger.info(f"Registered webhook for {topic} on {shop_domain}")
                else:
                    # Common idempotency response: address already taken
                    if response.status_code == 422:
                        try:
                            body = response.json() or {}
                            errors = body.get("errors") or {}
                            addr_errs = errors.get("address") or []
                            if isinstance(addr_errs, list) and any("already" in str(x).lower() for x in addr_errs):
                                already_exists.append(topic)
                                continue
                        except Exception:
                            pass

                    failed.append(
                        {
                            "topic": topic,
                            "status_code": response.status_code,
                            "body": (response.text or "")[:800],
                        }
                    )
                    logger.warning(f"Failed to register webhook for {topic}: {response.status_code} {response.text}")
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "registered_webhooks": registered,
            "already_exists": already_exists,
            "failed_webhooks": failed,
            "summary": {
                "requested": len(topics),
                "created": len(registered),
                "already_exists": len(already_exists),
                "failed": len(failed),
            },
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error registering Shopify webhooks: {e}")
        raise HTTPException(status_code=500, detail="Failed to register webhooks")


# ============================================================================
# Adyen Webhooks (TODO)
# ============================================================================

@router.post("/adyen")
async def handle_adyen_webhook(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(adyen_webhook_security),
):
    """
    Canonical Adyen webhook endpoint.

    `/psp/webhook/adyen` remains as a compatibility alias but delegates here.
    """
    from routes.psp_routes import process_adyen_webhook_request

    return await process_adyen_webhook_request(request, credentials)
