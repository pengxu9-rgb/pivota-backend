"""
Webhook 处理路由
处理来自 PSP（Stripe/Adyen）和 MCP（Shopify）的事件通知
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Header, Response, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import urlparse
import stripe
import os
import hmac
import hashlib
import json
import re
import socket
from datetime import datetime
from decimal import Decimal

from db.orders import get_order, update_order, update_order_status, mark_order_paid, mark_order_shipped
from db.merchant_onboarding import get_merchant_onboarding
from utils.auth import get_current_employee
from db.products import log_order_event
from config.platform import is_deployed, is_production
from config.settings import settings
from utils.logger import logger
from services.dispute_records_service import stripe_dispute_pack_status
from services.shopify_webhook_ingest import verify_shopify_hmac, ingest_shopify_webhook
from services.commerce_attribution_service import (
    close_external_order_conversion,
    extract_click_id_from_note_attributes,
    shopify_order_total_to_cents,
)
from services.catalog_sync_service import (
    create_catalog_sync_job,
    record_catalog_sync_event,
)
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
from services.webhook_service import WebhookService
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


# ---------------------------------------------------------------------------
# The two environment gates this module hangs security on, hoisted out of the
# route bodies so they can be tested directly.
#
# They were inline expressions inside 700-line async handlers, which meant the
# only way to exercise them was to drive a whole webhook request — so nobody
# did, and tests/test_platform_guard_parity.py never imported this module at
# all. Two mutants proved the hole by surviving the FULL 10,794-test sweep:
# swapping is_deployed() for is_production() in _shopify_prod_runtime() turns
# Shopify HMAC verification from ENFORCED to SKIPPED on every staging
# deployment, and stops the persistence-failure hard-fail from firing — CI
# green throughout. is_deployed() vs is_production() IS the distinction, so it
# is named and pinned on both sides.
# ---------------------------------------------------------------------------


def _stripe_livemode_gate_active() -> bool:
    """Production refuses test-mode Stripe events.

    is_production() — not is_deployed(): a staging deployment SHOULD accept
    test-mode events, that is what staging is for.
    """
    return (
        os.getenv("ENVIRONMENT", "").lower() == "production"
        or is_production()
    )


def _shopify_prod_runtime() -> bool:
    """Strict Shopify webhook handling: HMAC enforced, persistence failures fatal.

    is_deployed() — not is_production(): the pre-shim expression was
    ``bool(os.getenv("RAILWAY_GIT_COMMIT_SHA"))``, which was true on Railway
    STAGING as well, so staging has always enforced signatures. Narrowing this
    to is_production() would silently accept unsigned webhooks on every staging
    revision.
    """
    return (
        os.getenv("APP_ENV", "").lower() == "production"
        or os.getenv("ENVIRONMENT", "").lower() == "production"
        # `bool(RAILWAY_GIT_COMMIT_SHA)` meant "deployed"; it is False on Cloud Run.
        or is_deployed()
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


# Sentinel for "argument not supplied" where None is itself a meaningful value.
_UNSET = object()


def _db_row_to_dict(row: Any) -> Any:
    if row is None or isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return dict(mapping)
        except Exception:
            pass
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {key: row[key] for key in keys()}
        except Exception:
            pass
    try:
        return dict(row)
    except Exception:
        return row


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
    # A paid/settled order must never be demoted to payment_failed by a stale or
    # mis-correlated payment_intent.payment_failed event (e.g. a failed event for
    # an earlier abandoned PI that shares this order's metadata.order_id).
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
    if status in {
        "paid",
        "completed",
        "fulfilled",
        "partially_refunded",
        "refunded",
        "cancelled",
    }:
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
    psp_id: Optional[str] = None,
    psp_owner_merchant_id: Any = _UNSET,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the order a Stripe refund event belongs to.

    Returns `(order, reject_reason)`. `reject_reason` is set ONLY when an order
    was found and then REFUSED — today that is the cross-tenant block. A plain
    miss is `(None, None)`, which preserves the historical behaviour that a
    refund for a payment_intent we do not track is a no-op success.

    `psp_id` (the webhook endpoint owner) enforces the same cross-tenant guard
    the payment branches use: a merchant who knows their own endpoint secret must
    not be able to drive refund state on another merchant's order, whether by
    replaying a foreign payment_intent id or by forging metadata.order_id.

    `psp_owner_merchant_id` lets a caller that has ALREADY resolved the owner
    pass it in, so the dispute branch does not repeat an identical lookup.
    """
    psp_owner = (
        await _stripe_psp_owner_merchant_id(psp_id)
        if psp_owner_merchant_id is _UNSET
        else psp_owner_merchant_id
    )

    def _scoped(order: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        return _scope_stripe_order_to_psp_owner(
            order,
            psp_owner_merchant_id=psp_owner,
            psp_id=psp_id,
            payment_intent_id=payment_intent_id,
        )

    query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
    from db.database import database

    if payment_intent_id:
        result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
        if result:
            return _scoped(_db_row_to_dict(result))

    if isinstance(refund_meta, dict):
        order_hint = str(refund_meta.get("order_id") or "").strip()
        if order_hint:
            return _scoped(_db_row_to_dict(await get_order(order_hint)))
    return None, None


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


class _StripePspOwnerUnresolved(Exception):
    """The webhook path carried a `psp_id` whose owning merchant could not be
    determined — the lookup raised, returned no row, or returned an empty
    merchant_id.

    WHY THIS FAILS CLOSED. `_order_belongs_to_psp_owner` treats "no owner" as
    "no scope to enforce", which is correct ONLY for the bare `/stripe` endpoint
    authenticated by the platform-wide secret. On a per-psp endpoint that same
    `None` is indistinguishable from a transient DB failure — so returning it
    made ONE failed query silently restore the full pre-guard cross-tenant
    exposure (a reviewer reproduced the complete exploit by timing out this one
    statement, which is not hypothetical while the web pool is flapping with
    statement-timeout cancels).

    A psp_id that resolves to no owner is ALWAYS an error state, never a
    legitimate platform-wide call: `merchant_psps.psp_id` is the PRIMARY KEY and
    `merchant_id` is NOT NULL, so a non-empty psp_id in the path is positive
    evidence that an owner exists.

    The handler turns this into a 503 so STRIPE RETRIES. That matters: for the
    transient case, Stripe's own retry schedule is a real recovery net, whereas
    the 200-plus-'unmatched' path has no consumer and drops the event for good.
    """

    def __init__(self, psp_id: Optional[str], reason: str) -> None:
        super().__init__(f"psp_owner_unresolved:{reason}:psp_id={psp_id}")
        self.psp_id = psp_id
        self.reason = reason


async def _stripe_psp_owner_merchant_id(psp_id: Optional[str]) -> Optional[str]:
    """Return the merchant_id that owns this Stripe psp_id.

    `None` means "there is no psp scope to enforce" and is returned ONLY for a
    bare `/stripe` endpoint (no psp_id in the path). Every other unresolved
    outcome raises `_StripePspOwnerUnresolved` — see that class for why.
    """
    if not psp_id:
        return None

    try:
        from db.database import database

        row = await database.fetch_one(
            "SELECT merchant_id FROM merchant_psps WHERE psp_id = :psp_id AND provider = 'stripe' LIMIT 1",
            {"psp_id": psp_id},
        )
    except Exception as exc:
        logger.error(
            {
                "alert": "stripe_webhook_psp_owner_lookup_failed",
                "psp_id": psp_id,
                "error": str(exc)[:200],
                "impact": "refusing the event rather than falling back to an unscoped guard",
            }
        )
        raise _StripePspOwnerUnresolved(psp_id, "lookup_failed") from exc

    try:
        owner = str(row["merchant_id"] or "").strip() if row else ""
    except Exception as exc:  # row present but without the column we selected
        logger.error(
            {
                "alert": "stripe_webhook_psp_owner_lookup_failed",
                "psp_id": psp_id,
                "error": str(exc)[:200],
                "impact": "refusing the event rather than falling back to an unscoped guard",
            }
        )
        raise _StripePspOwnerUnresolved(psp_id, "unreadable_row") from exc

    if not owner:
        reason = "no_row" if not row else "empty_merchant_id"
        logger.error(
            {
                "alert": "stripe_webhook_psp_owner_missing",
                "psp_id": psp_id,
                "reason": reason,
                "impact": "refusing the event; a psp_id in the path must have an owner",
            }
        )
        raise _StripePspOwnerUnresolved(psp_id, reason)
    return owner


def _scope_stripe_order_to_psp_owner(
    order: Optional[Dict[str, Any]],
    *,
    psp_owner_merchant_id: Optional[str],
    psp_id: Optional[str],
    payment_intent_id: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Apply the cross-tenant guard to a resolved order and log the block.

    Shared by the payment and refund resolvers so both surfaces enforce (and
    alert on) the guard identically. Returns `(order, reject_reason)`.
    """
    if order is None:
        return None, None
    if not _order_belongs_to_psp_owner(order, psp_owner_merchant_id):
        logger.error(
            {
                "alert": "stripe_webhook_cross_tenant_blocked",
                "psp_id": psp_id,
                "psp_owner_merchant_id": psp_owner_merchant_id,
                "order_id": order.get("order_id"),
                "order_merchant_id": order.get("merchant_id"),
                "payment_intent_id": payment_intent_id,
            }
        )
        return None, "cross_tenant_blocked"
    return order, None


def _order_belongs_to_psp_owner(order: Dict[str, Any], psp_owner_merchant_id: Optional[str]) -> bool:
    """Cross-tenant guard: the resolved order must belong to the merchant that
    owns the webhook endpoint's psp_id. Skipped when psp_owner is unknown (bare
    /stripe endpoint authenticated by the platform-wide secret)."""
    if not psp_owner_merchant_id:
        return True
    return str(order.get("merchant_id") or "").strip() == psp_owner_merchant_id


async def _resolve_stripe_order_for_payment_event(
    *,
    payment_intent_id: Optional[str],
    payment_meta: Optional[Dict[str, Any]],
    allow_repoint: bool = False,
    psp_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the order a Stripe payment event belongs to.

    Lookup order: (1) by stored payment_intent_id, then (2) by the PI's
    metadata.order_id hint.

    `allow_repoint` controls whether, on the metadata-hint path, we overwrite the
    order's stored payment_intent_id with the event's PI. This is needed for the
    hosted-checkout success path (the order stores a `cs_…` Checkout Session id,
    while payment_intent.succeeded carries the `pi_…`). It is DANGEROUS on the
    failure path: a stale payment_intent.payment_failed for an abandoned PI would
    repoint a paid order off its real PI. So callers pass allow_repoint=True ONLY
    for success/capture events, never for failure events.

    `psp_id` (the webhook endpoint owner) enforces a cross-tenant guard: a
    merchant who knows their own endpoint secret cannot drive state on another
    merchant's order by forging metadata.order_id.

    Returns `(order, reject_reason)`. `reject_reason` is set ONLY when an order
    was found and REFUSED (the cross-tenant block); a plain miss is
    `(None, None)`. Callers need that distinction: a refusal is PERMANENT, while
    a miss is usually an order that has not been committed yet — and those two
    want opposite delivery outcomes. They used to be indistinguishable, both
    recorded as `no_order_resolved`.
    """
    psp_owner = await _stripe_psp_owner_merchant_id(psp_id)

    def _scoped(
        order: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        return _scope_stripe_order_to_psp_owner(
            order,
            psp_owner_merchant_id=psp_owner,
            psp_id=psp_id,
            payment_intent_id=payment_intent_id,
        )

    query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
    from db.database import database

    if payment_intent_id:
        result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
        if result:
            return _scoped(_db_row_to_dict(result))

    order_hint = ""
    if isinstance(payment_meta, dict):
        order_hint = str(payment_meta.get("order_id") or "").strip()
    if not order_hint:
        return None, None

    order = await get_order(order_hint)
    if not order:
        return None, None
    order = _db_row_to_dict(order)
    scoped, reject = _scoped(order)
    if scoped is None:
        return None, reject

    current_payment_intent_id = str(order.get("payment_intent_id") or "").strip()
    if allow_repoint and payment_intent_id and current_payment_intent_id != payment_intent_id:
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
                return _scoped(_db_row_to_dict(refreshed))
        except Exception:
            pass

    return order, None


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
    finalization = await finalize_refund_success(
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
    # FIX-05 C5: PSP-initiated refunds must reverse attribution like app-initiated do.
    if os.getenv("ATTRIBUTION_REVERSE_ON_REFUND", "true").strip().lower() != "false":
        try:
            from services.commerce_attribution_service import attach_refund_to_attribution_edge

            # MAJOR units. attach_refund_to_attribution_edge does
            # `amount_cents = amount * 100`, so passing the minor-unit value
            # recorded a 100x refund — and because
            # net_attributed_gmv_cents = GREATEST(gross - refund, 0) is a stored
            # generated column read by monthly_brand_statements_service, that
            # clamps the edge to zero and drops it from the merchant's invoice.
            # Never observable before: the statement failed to PREPARE, so this
            # never ran. See the same conversion at _stripe_minor_unit_factor
            # use below.
            await attach_refund_to_attribution_edge(
                order_id=str(order.get("order_id") or ""),
                refund_id=refund_reference,
                amount=(
                    Decimal(str(refund_amount_minor or "0"))
                    / _stripe_minor_unit_factor(currency or str(order.get("currency") or ""))
                ),
            )
        except Exception as edge_exc:
            logger.warning(
                {
                    "event": "stripe_refund_attribution_edge_attach_failed",
                    "order_id": str(order.get("order_id") or ""),
                    "refund_id": refund_reference,
                    "error": str(edge_exc),
                }
            )
    return finalization


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


def _stripe_webhook_event_id(event: Dict[str, Any], payload: bytes, event_type: Optional[str]) -> str:
    event_id = str((event or {}).get("id") or "").strip()
    if event_id:
        return event_id
    digest = hashlib.sha256(payload or b"").hexdigest()
    return f"stripe:{event_type or 'unknown'}:{digest}"


async def _record_stripe_webhook_event_best_effort(
    *,
    event_id: str,
    event_type: Optional[str],
    payload: Dict[str, Any],
    request_headers: Dict[str, str],
    signature_verified: bool,
    signature_header: Optional[str],
) -> bool:
    """
    Returns True when this event was already processed and should be skipped.
    Stripe already retries failed deliveries, so event persistence is best-effort and must not
    turn a valid PSP event into a 500 solely because the audit table is unavailable.
    """
    try:
        is_duplicate, _existing = await WebhookService.check_duplicate_event(event_id)
        if is_duplicate:
            return True
        await WebhookService.record_webhook_event(
            event_id=event_id,
            event_type=str(event_type or "unknown"),
            psp_type="stripe",
            order_id=None,
            payload=payload,
            headers=request_headers,
            signature_verified=signature_verified,
            signature_header=signature_header,
            status="pending",
        )
    except Exception as exc:
        logger.warning(
            "Stripe webhook idempotency persistence unavailable; continuing event_id=%s err=%s",
            event_id,
            exc,
        )
    return False


async def _mark_stripe_webhook_event_status_best_effort(
    event_id: Optional[str],
    status: str,
    error_message: Optional[str] = None,
) -> None:
    if not event_id:
        return
    try:
        await WebhookService.update_event_status(event_id, status, error_message)
    except Exception as exc:
        logger.warning(
            "Stripe webhook status update failed event_id=%s status=%s err=%s",
            event_id,
            status,
            exc,
        )


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


def _stripe_event_payment_matches_order(
    order: Dict[str, Any], data: Dict[str, Any]
) -> Tuple[bool, Optional[str]]:
    """Verify the signed Stripe event's charged amount + currency match the order.

    The event is signature-verified, so its amount/currency ARE the real charge.
    We must still confirm the charge corresponds to THIS order's total before
    marking it paid + fulfilling — otherwise a PI carrying the right
    metadata.order_id but a different amount (e.g. a $1 charge against a $500
    order) would fulfill at the wrong price. Returns (ok, reason_if_not).
    """
    order_currency = str(order.get("currency") or "").strip().lower()
    event_currency = str(data.get("currency") or "").strip().lower()
    if order_currency and event_currency and order_currency != event_currency:
        return False, f"currency_mismatch:order={order_currency},event={event_currency}"

    # Prefer amount_received (actually captured); fall back to amount (intended).
    observed_minor = data.get("amount_received")
    if observed_minor is None:
        observed_minor = data.get("amount")
    if observed_minor is None:
        return False, "event_amount_missing"

    order_total = order.get("total")
    if order_total is None:
        return False, "order_total_missing"

    try:
        factor = _stripe_minor_unit_factor(event_currency or order_currency)
        expected_minor = (Decimal(str(order_total)) * factor).to_integral_value()
        observed = Decimal(str(observed_minor))
    except Exception as exc:  # noqa: BLE001
        return False, f"amount_parse_error:{exc}"

    if observed != expected_minor:
        return (
            False,
            f"amount_mismatch:expected_minor={expected_minor},observed_minor={observed}",
        )
    return True, None


def _stripe_event_refund_matches_order(
    order: Dict[str, Any],
    *,
    refund_amount_minor: Any,
    currency: Optional[str],
    cumulative_total: Optional[Decimal] = None,
) -> Tuple[bool, Optional[str]]:
    """Verify the signed Stripe refund event's amount + currency are consistent
    with the order it claims to refund.

    The payment branches verify the charge against the order total before marking
    it paid; the refund branches used to apply whatever amount the event carried.
    The event IS signature-verified, so the amount is a real PSP amount — but it
    still has to belong to THIS order. A refund larger than the order total (or in
    a different currency) writes a bogus `total_refunded` and flips the order to
    `refunded`/`partially_refunded` on an amount we never charged.

    `cumulative_total`, when given, is the total that will actually be WRITTEN —
    the sum across this order's individual refunds. It, not the single amount, is
    what must fit inside the order total. Bounding only the single amount was
    correct while `refund_total` was a monotonic ceiling; once refund-level events
    started contributing a SUM, two $400 refunds on a $500 order each passed the
    per-refund check and wrote total_refunded=800 — a number that feeds
    `attach_refund_to_attribution_edge` and the merchant's statement.

    Returns (ok, reason_if_not).
    """
    order_currency = str(order.get("currency") or "").strip().lower()
    event_currency = str(currency or "").strip().lower()
    if order_currency and event_currency and order_currency != event_currency:
        return False, f"refund_currency_mismatch:order={order_currency},event={event_currency}"

    if refund_amount_minor is None:
        return False, "refund_amount_missing"

    order_total = order.get("total")
    if order_total is None:
        return False, "order_total_missing"

    try:
        factor = _stripe_minor_unit_factor(event_currency or order_currency)
        observed = Decimal(str(refund_amount_minor)) / factor
        max_refundable = Decimal(str(order_total))
    except Exception as exc:  # noqa: BLE001
        return False, f"refund_amount_parse_error:{exc}"

    if observed <= Decimal("0"):
        return False, f"refund_amount_not_positive:observed={observed}"

    bounded = observed if cumulative_total is None else cumulative_total
    if bounded > max_refundable:
        return (
            False,
            f"refund_exceeds_order_total:order_total={max_refundable},observed={bounded}",
        )
    return True, None


_STRIPE_REFUND_LEVEL_SOURCE_EVENTS = frozenset({"refund.updated"})


def _stripe_refund_level_cumulative(
    existing_metadata: Optional[Dict[str, Any]],
    *,
    refund_id: Optional[str],
    refund_total: Decimal,
) -> Decimal:
    """Total refunded across INDIVIDUAL refunds, folding in the one just received.

    WHY THIS EXISTS. `finalize_refund_success` applies `refund_total` as a
    MONOTONIC CEILING — `max(current_total_refunded, refund_total)` — not an
    accumulator. That suits `charge.refunded`, whose `amount_refunded` is the
    charge's cumulative total. `refund.updated` carries ONE refund's amount, so:

      - passing that single amount under-counts sequential partials: $300 then
        $200 on a $500 order lands at 300, and the order stays
        `partially_refunded`.
      - passing a naive running sum DOUBLE-counts, because `charge.refunded` may
        already have contributed the same money under a different refund key
        (`stripe:ch_…` vs `stripe:re_…`), which the `psp_refund_refs` duplicate
        guard therefore does not catch — verified to land at 600.

    So sum only the REFUND-LEVEL rows and hand the result to the ceiling.

    WHY psp_refund_records AND NOT A DEDICATED KEY. A first cut kept its own
    `stripe_refund_ledger` blob, which added a SECOND store with the same
    weakness. psp_refund_records at least already exists, is stamped by the call
    sites, and is pruned on rollback by the failure path — so this needs no
    bookkeeping of its own.

    ⚠️ IT IS NOT CONCURRENCY-SAFE, and this derivation inherits that. Order
    metadata is read at request start and written back whole:
    `update_order` replaces the entire column (db/orders.py), and
    `update_order_status` merges only at the TOP level
    (`{**existing_metadata, **update_data["metadata"]}`) — `psp_refund_records` is
    a nested dict supplied by the caller, so it is replaced wholesale either way.
    Two overlapping refund events, or a `refund.created` whose snapshot predates a
    `refund.updated` write, therefore lose a row.

    The failure is ONE-DIRECTIONAL: rows can only be lost, never duplicated, and
    `total_refunded` is a real column that survives, so a lost row degrades this
    to the pre-existing under-count rather than inventing money. That makes
    concurrent delivery no worse than before this change — but it is NOT the
    guarantee serialized delivery gets. Durable per-refund storage (a row per
    refund, or a JSONB sub-key merge done in SQL) is the actual fix and is
    tracked separately.
    """
    records: Dict[str, Any] = {}
    meta = existing_metadata if isinstance(existing_metadata, dict) else {}
    raw = meta.get("psp_refund_records")
    if isinstance(raw, dict):
        records = raw

    this_refund = str(refund_id or "").strip()
    cumulative = refund_total
    for record in records.values():
        if not isinstance(record, dict):
            continue
        if str(record.get("source_event") or "") not in _STRIPE_REFUND_LEVEL_SOURCE_EVENTS:
            continue
        # This refund's own prior row is superseded by the amount just received.
        # Compared directly rather than gated on a non-empty id: an id-less refund
        # writes an id-less row, and skipping the comparison made its redelivery
        # sum that row on top of itself.
        if str(record.get("refund_reference") or "").strip() == this_refund:
            continue
        # `amount_minor` is the event's FACE amount, passed straight through by the
        # finalizer. Do NOT use `amount`: that is the delta actually applied, which
        # the cumulative branch recomputes as
        # `next_total_refunded - current_total_refunded` and therefore ZEROES on a
        # redelivery — summing it silently loses that refund's money.
        raw_minor = record.get("amount_minor")
        if raw_minor is None:
            continue
        try:
            record_factor = _stripe_minor_unit_factor(str(record.get("currency") or ""))
            cumulative += Decimal(str(raw_minor)) / record_factor
        except Exception:  # noqa: BLE001 - a malformed row must not wedge refunds
            logger.warning(
                {
                    "alert": "stripe_refund_record_unparsable_amount",
                    "refund_reference": str(record.get("refund_reference") or "")[:64],
                }
            )
    return cumulative


# Refusal reasons that can NEVER succeed on redelivery. Everything else is
# treated as possibly-transient and handed to Stripe's retry schedule.
_STRIPE_PERMANENT_REFUSAL_PREFIXES = (
    "cross_tenant_blocked",
    "refund_currency_mismatch",
    "currency_mismatch",
    "amount_mismatch",
    "refund_exceeds_order_total",
    "refund_amount_not_positive",
    "event_amount_missing",
    "refund_amount_missing",
    # Deterministic in the signed bytes plus the order row: an identical
    # redelivery reproduces them exactly, so retrying can only burn the schedule.
    "amount_parse_error",
    "refund_amount_parse_error",
)


def _stripe_refusal_is_permanent(reason: Optional[str]) -> bool:
    """Is this refusal one that redelivering the identical event cannot fix?

    A cross-tenant block or an amount that does not match the order is a
    property of the signed event itself — the same bytes will be refused
    forever, so retrying is pure noise. A miss (`no_order_resolved`) is usually
    a RACE: the charge landed before the order was committed. Those want
    opposite outcomes, and until now both answered 200 and were recorded
    'unmatched', where nothing ever looked at them again.
    """
    text = str(reason or "").strip()
    if not text:
        return False
    return text.startswith(_STRIPE_PERMANENT_REFUSAL_PREFIXES)


# `db.orders.create_order` mints `ORD_<16 uppercase hex>`. Used to tell an event
# that names one of OUR orders from one carrying some other system's order_id.
_PIVOTA_ORDER_ID_SHAPE = re.compile(r"^ORD_[0-9A-F]{16}$")


def _stripe_event_names_a_pivota_order(payment_meta: Optional[Dict[str, Any]]) -> bool:
    """Does this event's metadata name an order that could be ours?

    `order_id` in PaymentIntent metadata is a WooCommerce/Magento/custom-cart
    convention, not a Pivota marker — a merchant's own storefront charge on their
    own Stripe account carries one too. Matching on mere PRESENCE would defer
    those for the full retry schedule; matching on our id shape does not.

    Unknown shapes fail CLOSED to the historical 200, so a legacy order id we no
    longer mint is never deferred.
    """
    if not isinstance(payment_meta, dict):
        return False
    hint = str(payment_meta.get("order_id") or "").strip()
    return bool(_PIVOTA_ORDER_ID_SHAPE.match(hint))


def _stripe_unmatched_response(
    *,
    event_type: Optional[str],
    reason: Optional[str],
    claims_our_order: bool = False,
) -> Dict[str, Any]:
    """The delivery outcome for a refused event.

    Permanent refusal -> 200 + 'unmatched'. Retrying cannot help, and a 200 stops
    Stripe hammering an endpoint over an event it will always refuse.

    Possibly-transient refusal -> 503, so STRIPE REDELIVERS. This is the whole
    recovery net: `webhook_events.status = 'unmatched'` has no consumer in this
    repo, so a 200 here means a real charge or refund is dropped for good. Stripe
    already retries with backoff for ~3 days; using that beats inventing a sweep
    that does not exist.

    `claims_our_order` GATES that deferral, and it is not optional. A per-psp
    endpoint is created on the MERCHANT'S OWN Stripe account
    (`_ensure_stripe_webhook_endpoint`, `stripe_account=account_id`) and
    subscribes to `payment_intent.succeeded` among others. Stripe endpoints are
    account-wide, not order-scoped, so every charge that merchant takes OUTSIDE
    Pivota — their own storefront, invoices, subscriptions — is delivered here and
    resolves to no order. Deferring those would answer 503 to events that can
    never resolve, on the full retry schedule, and Stripe disables endpoints that
    fail continuously. Losing the endpoint would take down payment finalization
    AND refunds for that merchant: strictly worse than the dropped event this is
    meant to fix. So only an event whose metadata names an order matching OUR id
    shape is deferred; anything else keeps the historical 200.

    Residual, accepted: a succeeded PI for a SOFT-DELETED Pivota order matches the
    shape and never resolves (`get_order` filters `is_deleted`), so it defers for
    the full schedule. That is one bounded event, not the per-charge volume that
    threatens the endpoint, and it fails visibly rather than silently.
    """
    if _stripe_refusal_is_permanent(reason) or not claims_our_order:
        return {"status": "unmatched", "event": event_type, "reason": reason}
    logger.error(
        {
            "alert": "stripe_webhook_deferred_for_redelivery",
            "event_type": event_type,
            "reason": reason,
            "impact": "answered 503; Stripe will redeliver on its retry schedule",
        }
    )
    raise HTTPException(status_code=503, detail=str(reason or "unmatched"))


async def _flag_unmatched_stripe_refund_event(
    *,
    event_id: Optional[str],
    event_type: Optional[str],
    payment_intent_id: Optional[str],
    refund_reference: Optional[str],
    reason: str,
) -> None:
    """A signed refund event resolved to an order we REFUSED to mutate — either
    the cross-tenant guard blocked it, or its amount/currency does not match the
    order. The refund is real at the PSP, so recording the event as 'processed'
    would sweep it under the rug. Record 'unmatched' and alert loudly.

    ⚠️ THERE IS NO AUTOMATED RECOVERY NET. `webhook_events.status == 'unmatched'`
    has no consumer anywhere in this repo, and we answer 200, so Stripe treats
    the delivery as successful and never retries. Recovery today is a human
    resending the event from the Stripe dashboard (`check_duplicate_event`
    counts only 'processed'/'ignored' as duplicates, so a resend does
    reprocess). Until a sweep exists, this alert is the ONLY signal."""
    logger.error(
        {
            "alert": "stripe_refund_event_unmatched",
            "event_type": event_type,
            "event_id": event_id,
            "payment_intent_id": payment_intent_id,
            "refund_reference": refund_reference,
            "reason": reason,
            "impact": "refund was NOT applied to any order; reconcile required",
        }
    )
    await _mark_stripe_webhook_event_status_best_effort(event_id, "unmatched", reason)


async def _flag_unmatched_stripe_payment_event(
    *,
    event_id: Optional[str],
    event_type: Optional[str],
    payment_intent_id: Optional[str],
    payment_meta: Optional[Dict[str, Any]],
    reason: str,
) -> None:
    """A signed payment SUCCESS event resolved to no order (or failed integrity
    verification). This is the charge-stuck failure mode: a real charge with no
    finalizable order. Record the event as 'unmatched' (NOT 'processed', so it is
    never silently swept under the rug) and emit a loud alert.

    ⚠️ There is NO reconcile sweep — `webhook_events.status = 'unmatched'` has no
    consumer anywhere in this repo. Recovery for a POSSIBLY-TRANSIENT refusal is
    Stripe's own redelivery, which `_stripe_unmatched_response` triggers with a
    503. A permanent refusal genuinely ends here, and the alert above is the only
    signal."""
    meta_order_id = None
    if isinstance(payment_meta, dict):
        meta_order_id = str(payment_meta.get("order_id") or "").strip() or None
    logger.error(
        {
            "alert": "stripe_payment_event_unmatched",
            "event_type": event_type,
            "event_id": event_id,
            "payment_intent_id": payment_intent_id,
            "metadata_order_id": meta_order_id,
            "reason": reason,
            "impact": "charge may have succeeded with no finalizable order; NO automated sweep exists — if this was not deferred for Stripe redelivery it needs manual follow-up",
        }
    )
    await _mark_stripe_webhook_event_status_best_effort(event_id, "unmatched", reason)


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
    - payment_intent.amount_capturable_updated: manual-capture authorization ready
    - payment_intent.payment_failed: 支付失败
    - refund.created / refund.updated / refund.failed: 退款时间线
    - charge.refunded: 退款成功
    - charge.dispute.*: 争议/拒付（chargeback）信号（best-effort 记录，不自动变更订单状态）
    """
    stripe_webhook_event_id: Optional[str] = None
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
            # No Stripe webhook secret configured for this psp_id. We REFUSE to
            # fail open in ANY environment. Accepting unsigned payloads in
            # dev/staging used to be the convenience escape hatch, but staging
            # shares the production Postgres (single-DB tenancy), so an unsigned
            # event accepted on staging mutates real production orders. Configure
            # STRIPE_WEBHOOK_SECRET (or the per-psp webhook_endpoint_secret) for
            # every deployment, including local, to exercise this path.
            logger.error(
                "Stripe webhook secret not configured for psp_id=%s — rejecting "
                "unsigned event (signature is mandatory in all environments)",
                psp_id,
            )
            raise HTTPException(status_code=503, detail="webhook_secret_not_configured")
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

        # Livemode gate: in production, refuse test-mode events. A test-mode
        # endpoint secret (per-psp or platform) that happens to verify must not
        # be able to mutate live orders. `livemode` is part of the signed event.
        is_prod_env = _stripe_livemode_gate_active()
        event_livemode = event.get("livemode")
        if is_prod_env and event_livemode is False:
            logger.warning(
                "Ignoring test-mode Stripe webhook (livemode=false) in production: type=%s id=%s",
                event_type,
                event.get("id"),
            )
            return {"status": "ignored", "event": event_type, "reason": "test_mode_event_in_production"}

        stripe_webhook_event_id = _stripe_webhook_event_id(event, payload, event_type)
        is_duplicate = await _record_stripe_webhook_event_best_effort(
            event_id=stripe_webhook_event_id,
            event_type=event_type,
            payload=event,
            request_headers=dict(request.headers),
            signature_verified=bool(secret_candidates),
            signature_header=stripe_signature,
        )
        if is_duplicate:
            logger.info("Duplicate Stripe webhook skipped: %s", stripe_webhook_event_id)
            return {"status": "success", "event": event_type, "duplicate": True}
        
        if event_type == "payment_intent.succeeded":
            # 支付成功
            payment_intent_id = data.get("id")
            payment_meta = _stripe_object_to_dict(data.get("metadata") or {})
            # allow_repoint=True: hosted-checkout orders store the cs_ session id;
            # the success event carries the pi_, so capturing it is correct here.
            result, payment_reject = await _resolve_stripe_order_for_payment_event(
                payment_intent_id=payment_intent_id,
                payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                allow_repoint=True,
                psp_id=psp_id,
            )

            if not result:
                # Signed success event with NO finalizable order (orphaned
                # metadata.order_id, cross-tenant block, or order not yet
                # committed). Do NOT mark 'processed' and swallow it — flag it.
                await _flag_unmatched_stripe_payment_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                    reason=payment_reject or "no_order_resolved",
                )
                return _stripe_unmatched_response(
                    event_type=event_type,
                    reason=payment_reject or "no_order_resolved",
                    # Only defer when the event names one of OUR orders; see the
                    # account-wide-endpoint note in _stripe_unmatched_response.
                    claims_our_order=_stripe_event_names_a_pivota_order(payment_meta),
                )

            # Integrity: the signed charge amount/currency must match the order.
            amount_ok, amount_reason = _stripe_event_payment_matches_order(result, data)
            if not amount_ok:
                await _flag_unmatched_stripe_payment_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                    reason=amount_reason or "amount_verification_failed",
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=amount_reason or "amount_verification_failed"
                )

            order_id = result["order_id"]
            merchant_id = result["merchant_id"]
            finalization = await _finalize_stripe_payment_success(
                result,
                payment_intent_id=payment_intent_id,
                data=data,
            )
            # Gate one-time side effects on `transitioned`: only the finalizer call
            # that actually flipped this order to paid (atomic in mark_order_paid)
            # fulfills + notifies the merchant, so a concurrent finalize (sync
            # confirm / reconcile sweep) cannot double-fulfill.
            if finalization.get("transitioned"):
                logger.info(f"Order {order_id} marked as paid via webhook")

                # Decision-layer outcome join: link the settled sale back to the
                # decision that produced it (agent_decision_funnel_links). Gated on
                # `transitioned` so it fires exactly once (same atomic single-fire
                # guard as Shopify-order-creation and the merchant webhook). Best-
                # effort + decoupled (record_funnel_link enqueues to an async writer
                # with ON CONFLICT (funnel_event_id) DO NOTHING), so it can neither
                # roll back the paid commit nor double-write.
                try:
                    from services.agent_decision_event_store import (
                        extract_order_decision_linkage,
                        record_funnel_link,
                    )
                    from services.commerce_attribution_service import (
                        get_order_attribution_edge_id,
                    )

                    linkage = extract_order_decision_linkage(result.get("metadata"))
                    funnel_event_ids = finalization.get("funnel_event_ids") or []
                    if linkage.get("decision_id") or linkage.get("checkout_decision_id"):
                        if not funnel_event_ids:
                            logger.warning(
                                "Paid order %s has decision linkage but no funnel_event_id "
                                "to join (decision_id=%s checkout_decision_id=%s)",
                                order_id,
                                linkage.get("decision_id"),
                                linkage.get("checkout_decision_id"),
                            )
                        # P0.3: bridge the decision funnel link to the GMV-bearing
                        # attribution edge. FK-safe — resolves only an EXISTING
                        # edge_id (ON DELETE SET NULL), so an order with no edge
                        # (direct checkout, no attribution signal) threads None
                        # and the link still records. This is what lets
                        # outcome_aggregation value the decision via the edge.
                        edge_id = await get_order_attribution_edge_id(order_id)
                        # content_key / catalog_offer_id are deliberately omitted:
                        # they're ON DELETE RESTRICT FKs and a stale value would
                        # abort the whole link row, dropping an otherwise-valid
                        # decision join. They're recoverable downstream via the
                        # checkout_decisions / agent_decision_candidates rows.
                        link_kwargs = {
                            "decision_id": linkage.get("decision_id"),
                            "checkout_decision_id": linkage.get("checkout_decision_id"),
                            "commerce_attribution_edge_id": edge_id,
                            "merchant_id": merchant_id,
                        }
                        if linkage.get("protocol"):
                            link_kwargs["protocol"] = linkage["protocol"]
                        for funnel_event_id in funnel_event_ids:
                            await record_funnel_link(
                                funnel_event_id=funnel_event_id, **link_kwargs
                            )
                except Exception as link_exc:  # noqa: BLE001
                    logger.warning(
                        "Decision funnel-link join failed for paid order %s: %s",
                        order_id,
                        link_exc,
                    )

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

        elif event_type == "payment_intent.amount_capturable_updated":
            payment_intent_id = data.get("id")
            payment_meta = _stripe_object_to_dict(data.get("metadata") or {})
            result, _payment_reject = await _resolve_stripe_order_for_payment_event(
                payment_intent_id=payment_intent_id,
                payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                allow_repoint=True,
                psp_id=psp_id,
            )
            if _payment_reject:
                await _flag_unmatched_stripe_payment_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                    reason=_payment_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=_payment_reject
                )

            if result:
                from routes.order_routes import finalize_authorized_payment_order

                auth_result = await finalize_authorized_payment_order(
                    str(result["order_id"]),
                    order=result,
                    source_event="stripe_amount_capturable_webhook",
                )
                logger.info(
                    "Stripe auth-first finalization for order %s returned %s",
                    result["order_id"],
                    auth_result.get("status"),
                )

        elif event_type == "checkout.session.completed":
            # Stripe Checkout manual-capture sessions can complete before the
            # amount_capturable webhook is processed. For explicitly marked
            # auth-first orders, run the same merchant-order-before-capture
            # finalizer; normal capture-first Checkout Sessions remain on the
            # existing payment_intent.succeeded path.
            session_id = data.get("id")
            payment_meta = _stripe_object_to_dict(data.get("metadata") or {})
            auth_first_hint = (
                isinstance(payment_meta, dict)
                and (
                    str(payment_meta.get("payment_flow") or "").strip().lower() == "authorization_first"
                    or str(payment_meta.get("capture_method") or "").strip().lower() == "manual"
                    or str(payment_meta.get("payment_capture_method") or "").strip().lower() == "manual"
                    or str(payment_meta.get("stripe_capture_method") or "").strip().lower() == "manual"
                )
            )
            if auth_first_hint:
                result, _payment_reject = await _resolve_stripe_order_for_payment_event(
                    payment_intent_id=session_id,
                    payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                    allow_repoint=True,
                    psp_id=psp_id,
                )
            else:
                result = None
                _payment_reject = None
            if _payment_reject:
                await _flag_unmatched_stripe_payment_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=session_id,
                    payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                    reason=_payment_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=_payment_reject
                )
            if result:
                from routes.order_routes import (
                    finalize_authorized_payment_order,
                    order_uses_authorization_first_payment,
                )

                if order_uses_authorization_first_payment(result):
                    auth_result = await finalize_authorized_payment_order(
                        str(result["order_id"]),
                        order=result,
                        source_event="stripe_checkout_session_completed_webhook",
                    )
                    logger.info(
                        "Stripe Checkout auth-first finalization for order %s returned %s",
                        result["order_id"],
                        auth_result.get("status"),
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
            # allow_repoint stays False: a stale/abandoned failed PI must never
            # repoint (and then demote) a paid order via metadata.order_id.
            result, _payment_reject = await _resolve_stripe_order_for_payment_event(
                payment_intent_id=payment_intent_id,
                payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                allow_repoint=False,
                psp_id=psp_id,
            )

            if _payment_reject:
                await _flag_unmatched_stripe_payment_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    payment_meta=payment_meta if isinstance(payment_meta, dict) else None,
                    reason=_payment_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=_payment_reject
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
            
            # This branch used to run a raw
            # `SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id`
            # with NO psp scoping, so a merchant holding their own endpoint secret
            # could drive refund state on another merchant's order. It now goes
            # through the same guarded resolver the payment branches use.
            # `refund_meta=None` keeps the lookup surface exactly as narrow as it
            # was (payment_intent only — no metadata.order_id hint).
            result, refund_reject = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=None,
                psp_id=psp_id,
            )
            if refund_reject:
                await _flag_unmatched_stripe_refund_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    refund_reference=charge_id,
                    reason=refund_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=refund_reject
                )

            if result:
                # Integrity: unlike the payment branches, this path applied
                # whatever amount the event carried. Verify it against the order.
                amount_ok, amount_reason = _stripe_event_refund_matches_order(
                    result,
                    refund_amount_minor=refund_amount,
                    currency=currency,
                )
                if not amount_ok:
                    await _flag_unmatched_stripe_refund_event(
                        event_id=stripe_webhook_event_id,
                        event_type=event_type,
                        payment_intent_id=payment_intent_id,
                        refund_reference=charge_id,
                        reason=amount_reason or "refund_amount_verification_failed",
                    )
                    return _stripe_unmatched_response(
                        event_type=event_type, reason=amount_reason or "refund_amount_verification_failed"
                    )

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

            result, refund_reject = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=refund_meta if isinstance(refund_meta, dict) else None,
                psp_id=psp_id,
            )
            if refund_reject:
                await _flag_unmatched_stripe_refund_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    refund_reference=refund_id,
                    reason=refund_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=refund_reject
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

            result, refund_reject = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=refund_meta if isinstance(refund_meta, dict) else None,
                psp_id=psp_id,
            )
            if refund_reject:
                await _flag_unmatched_stripe_refund_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    refund_reference=refund_id,
                    reason=refund_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=refund_reject
                )

            if result:
                order_id = result["order_id"]
                existing_meta = result.get("metadata") or {}
                if not isinstance(existing_meta, dict):
                    existing_meta = {}

                if refund_status == "succeeded":
                    refund_factor = _stripe_minor_unit_factor(
                        currency or str(result.get("currency") or "")
                    )
                    try:
                        this_refund_total = (
                            Decimal(str(refund_amount)) / refund_factor
                            if refund_amount is not None
                            else Decimal("0")
                        )
                    except Exception:
                        this_refund_total = Decimal("0")
                    # This event carries ONE refund's amount, but refund_total is
                    # applied as a ceiling, so send the sum across this order's
                    # individual refunds — and bound THAT, not the single amount.
                    cumulative_refunded = _stripe_refund_level_cumulative(
                        existing_meta,
                        refund_id=refund_id,
                        refund_total=this_refund_total,
                    )
                    amount_ok, amount_reason = _stripe_event_refund_matches_order(
                        result,
                        refund_amount_minor=refund_amount,
                        currency=currency,
                        cumulative_total=cumulative_refunded,
                    )
                    if not amount_ok:
                        await _flag_unmatched_stripe_refund_event(
                            event_id=stripe_webhook_event_id,
                            event_type=event_type,
                            payment_intent_id=payment_intent_id,
                            refund_reference=refund_id,
                            reason=amount_reason or "refund_amount_verification_failed",
                        )
                        return _stripe_unmatched_response(
                            event_type=event_type, reason=amount_reason or "refund_amount_verification_failed"
                        )

                    await _finalize_stripe_refund_success(
                        result,
                        refund_reference=refund_id,
                        refund_amount_minor=refund_amount,
                        currency=currency or str(result.get("currency") or ""),
                        refund_total=cumulative_refunded,
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
            result, refund_reject = await _resolve_stripe_order_for_refund(
                payment_intent_id=payment_intent_id,
                refund_meta=refund_meta if isinstance(refund_meta, dict) else None,
                psp_id=psp_id,
            )
            if refund_reject:
                await _flag_unmatched_stripe_refund_event(
                    event_id=stripe_webhook_event_id,
                    event_type=event_type,
                    payment_intent_id=payment_intent_id,
                    refund_reference=refund_id,
                    reason=refund_reject,
                )
                return _stripe_unmatched_response(
                    event_type=event_type, reason=refund_reject
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
            #
            # ⚠️ ORDER STATUS mutation is gated by CHARGEBACK_REVERSE_ORDER_STATUS
            # (default off) — but that flag guards a branch that only LOGS
            # 'not_implemented_for_v1_dogfood'. The branch that actually WRITES is
            # gated by ATTRIBUTION_REVERSE_ON_CHARGEBACK, which defaults ON. Do not
            # read the first flag as a mitigation for this branch; it is not one.
            #
            # This branch used to take `order_id` and `merchant_id` STRAIGHT OUT OF
            # `data.metadata` — attacker-controlled on a signed event — and hand
            # them to three writers with no tenant predicate anywhere. That is the
            # same cross-tenant hole the refund branches had, against the same
            # attribution edge (attach_refund_to_attribution_edge's UPDATE is keyed
            # on order_id alone), which feeds the victim's monthly statement.
            # Identity now comes from the endpoint owner plus a SCOPED order
            # lookup; metadata is only ever a hint that must survive scoping.
            dispute_payload = {}
            if isinstance(data, dict):
                dispute_payload = data
            elif hasattr(data, "to_dict"):
                try:
                    dispute_payload = data.to_dict()
                except Exception:
                    dispute_payload = {}
            dispute_meta = {}
            if isinstance(dispute_payload, dict):
                raw_meta = dispute_payload.get("metadata") or {}
                dispute_meta = raw_meta if isinstance(raw_meta, dict) else {}
            dispute_id = str((dispute_payload or {}).get("id") or "").strip()
            raw_status = str((dispute_payload or {}).get("status") or "").strip().lower()

            dispute_psp_owner: Optional[str] = None
            if psp_id:
                # Per-merchant endpoint: this is the attack surface — the caller
                # authenticated with THEIR OWN secret, so their metadata must not
                # choose whose order gets touched.
                # Raises _StripePspOwnerUnresolved -> 503 rather than falling open.
                dispute_psp_owner = await _stripe_psp_owner_merchant_id(psp_id)
                dispute_pi = str((dispute_payload or {}).get("payment_intent") or "").strip() or None
                dispute_order, dispute_reject = await _resolve_stripe_order_for_refund(
                    payment_intent_id=dispute_pi,
                    refund_meta=dispute_meta,
                    psp_id=psp_id,
                    psp_owner_merchant_id=dispute_psp_owner,
                )
                if dispute_reject:
                    await _flag_unmatched_stripe_refund_event(
                        event_id=stripe_webhook_event_id,
                        event_type=event_type,
                        payment_intent_id=dispute_pi,
                        refund_reference=dispute_id,
                        reason=dispute_reject,
                    )
                    return _stripe_unmatched_response(
                        event_type=event_type, reason=dispute_reject
                    )

                if dispute_order:
                    # Resolved AND scoped: the object we are allowed to touch.
                    order_id = str(dispute_order.get("order_id") or "").strip() or None
                    merchant_id = str(dispute_order.get("merchant_id") or "").strip()
                else:
                    # No matching order under this tenant. The endpoint owner is
                    # authoritative for the dispute record; metadata is not.
                    # order_id stays None, which keeps every order-keyed writer
                    # below inert rather than pointing it at a foreign order.
                    order_id = None
                    merchant_id = dispute_psp_owner or ""
            else:
                # Bare /stripe endpoint, authenticated by the platform-wide
                # secret: there is no endpoint owner to scope to, the same open
                # posture the payment and refund guards take there. Unchanged.
                merchant_id = str(dispute_meta.get("merchant_id") or "").strip()
                order_id = str(dispute_meta.get("order_id") or "").strip() or None

            try:
                from services.dispute_records_service import (
                    stripe_dispute_status_detail,
                    upsert_stripe_dispute_record_best_effort,
                )

                await upsert_stripe_dispute_record_best_effort(
                    dispute_payload,
                    event_type=str(event_type),
                    order_id_hint=order_id,
                    merchant_id_hint=merchant_id or None,
                    # Without this the service falls back to its OWN unscoped
                    # `WHERE payment_intent_id = :pi` lookup and re-derives the
                    # foreign identity we just refused.
                    merchant_scope=dispute_psp_owner,
                )
            except Exception:
                pass
            try:
                from services.pcs_evidence_pack_service import create_dispute_evidence_pack

                dispute_status_detail = stripe_dispute_status_detail(raw=raw_status or None, event_type=event_type)
                if merchant_id and dispute_id:
                    await create_dispute_evidence_pack(
                        merchant_id=merchant_id,
                        dispute_ref=dispute_id,
                        order_id=order_id,
                        dispute_payload=dict(dispute_payload or {}),
                        source="stripe",
                        status=str(dispute_status_detail["pack_status"]),
                        event_type=str(event_type or "") or None,
                        triggered_by=f"stripe_webhook:{event_type}",
                    )
            except Exception:
                pass

            # FIX-05 C6: For dogfood we reverse attribution only; order status stays gated.
            if os.getenv("ATTRIBUTION_REVERSE_ON_CHARGEBACK", "true").strip().lower() != "false":
                try:
                    raw_dispute_amount = (dispute_payload or {}).get("amount")
                    dispute_amount_minor = (
                        int(Decimal(str(raw_dispute_amount)))
                        if raw_dispute_amount is not None
                        else None
                    )
                    if order_id and dispute_id and dispute_amount_minor and dispute_amount_minor > 0:
                        from services.commerce_attribution_service import attach_refund_to_attribution_edge

                        # MAJOR units — see the note on the refund path above.
                        await attach_refund_to_attribution_edge(
                            order_id=order_id,
                            refund_id=dispute_id,
                            amount=(
                                Decimal(str(dispute_amount_minor or "0"))
                                / _stripe_minor_unit_factor(
                                    str((dispute_payload or {}).get("currency") or "")
                                )
                            ),
                        )
                        await log_order_event(
                            event_type="chargeback_received",
                            order_id=order_id,
                            merchant_id=merchant_id,
                            metadata={"dispute_id": dispute_id, "amount": dispute_amount_minor},
                        )
                except Exception as edge_exc:
                    logger.warning(
                        {
                            "event": "stripe_chargeback_attribution_edge_attach_failed",
                            "order_id": order_id,
                            "dispute_id": dispute_id,
                            "error": str(edge_exc),
                        }
                    )

            if os.getenv("CHARGEBACK_REVERSE_ORDER_STATUS", "false").strip().lower() == "true":
                logger.info(
                    {
                        "event": "stripe_chargeback_order_status_reversal_skipped",
                        "order_id": order_id,
                        "dispute_id": dispute_id,
                        "reason": "not_implemented_for_v1_dogfood",
                    }
                )
        
        await _mark_stripe_webhook_event_status_best_effort(stripe_webhook_event_id, "processed")
        return {"status": "success", "event": event_type}
        
    except _StripePspOwnerUnresolved as exc:
        # Fail CLOSED, and answer 503 so Stripe redelivers. Do NOT return 200
        # with 'unmatched' here: that status has no consumer, so a transient DB
        # failure would silently discard a real payment/refund event.
        await _mark_stripe_webhook_event_status_best_effort(
            stripe_webhook_event_id, "failed", str(exc)
        )
        logger.error(
            {
                "alert": "stripe_webhook_refused_owner_unresolved",
                "psp_id": exc.psp_id,
                "reason": exc.reason,
                "impact": "answered 503; Stripe will redeliver",
            }
        )
        raise HTTPException(status_code=503, detail="psp_owner_unresolved")
    except HTTPException as exc:
        await _mark_stripe_webhook_event_status_best_effort(
            stripe_webhook_event_id,
            "failed",
            str(getattr(exc, "detail", None) or exc),
        )
        raise
    except Exception as e:
        await _mark_stripe_webhook_event_status_best_effort(stripe_webhook_event_id, "failed", str(e))
        logger.exception(f"Error handling Stripe webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Shopify GDPR Webhooks (static endpoint for Partner Dashboard configuration)
# ============================================================================

# PII keys stripped when scrubbing webhook-event payloads during redaction. Kept
# in sync with services/shopify_webhook_ingest._PII_KEYS (fix #6); duplicated here
# to scrub historical rows persisted BEFORE fix #6 landed.
_GDPR_SCRUB_KEYS = (
    "customer",
    "email",
    "contact_email",
    "phone",
    "billing_address",
    "shipping_address",
    "customer_locale",
    "browser_ip",
    "customer_url",
)

_REDACTED_NAME = "[REDACTED]"


def _redacted_email(original: Optional[str]) -> str:
    """
    Non-null tombstone for the non-nullable orders.customer_email column. Uses an
    sha256-8 of the original so redacted rows remain distinct (no collisions on a
    single sentinel) without retaining the address.
    """
    src = (original or "").strip().lower()
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:8] if src else "00000000"
    return f"redacted+{digest}@redacted.invalid"


def _scrub_webhook_event_payload(payload_json: Any) -> Any:
    """Strip PII keys from a stored webhook-event payload (dict-in, dict-out)."""
    if not isinstance(payload_json, dict):
        return payload_json
    for key in _GDPR_SCRUB_KEYS:
        payload_json.pop(key, None)
    line_items = payload_json.get("line_items")
    if isinstance(line_items, list):
        for item in line_items:
            if isinstance(item, dict):
                item.pop("destination_location", None)
                item.pop("origin_location", None)
    payload_json["pii_stripped"] = True
    return payload_json


async def _record_gdpr_request(
    *,
    merchant_id: Optional[str],
    shop_domain: Optional[str],
    topic: str,
    shopify_request: Dict[str, Any],
    status: str,
    resolution: Dict[str, Any],
) -> None:
    """Persist the compliance-request audit row (best-effort; never raises)."""
    try:
        from db.database import database

        await database.execute(
            """
            INSERT INTO shopify_gdpr_requests
              (merchant_id, shop_domain, topic, shopify_request, status, resolution, resolved_at)
            VALUES
              (:merchant_id, :shop_domain, :topic, CAST(:shopify_request AS jsonb),
               :status, CAST(:resolution AS jsonb),
               CASE WHEN :status IN ('completed', 'needs_review') THEN NOW() ELSE NULL END)
            """,
            {
                "merchant_id": merchant_id,
                "shop_domain": shop_domain,
                "topic": topic,
                "shopify_request": json.dumps(shopify_request or {}, ensure_ascii=False, default=str),
                "status": status,
                "resolution": json.dumps(resolution or {}, ensure_ascii=False, default=str),
            },
        )
    except Exception as e:
        logger.warning("Failed to persist shopify_gdpr_requests row topic=%s err=%s", topic, str(e)[:200])


async def _scrub_webhook_events_for_email(
    *, shop_domain: Optional[str], customer_email: Optional[str]
) -> int:
    """
    Scrub PII from pcs_shopify_webhook_events order rows for this shop whose
    payload contains the customer's email. Returns count of rows rewritten.
    """
    if not shop_domain or not customer_email:
        return 0
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT id, payload_json
        FROM pcs_shopify_webhook_events
        WHERE lower(shop_domain) = :shop_domain
          AND topic LIKE 'orders/%'
        """,
        {"shop_domain": shop_domain.strip().lower()},
    )
    target = customer_email.strip().lower()
    scrubbed = 0
    for row in rows or []:
        payload = row["payload_json"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if not isinstance(payload, dict):
            continue
        if target not in json.dumps(payload, ensure_ascii=False, default=str).lower():
            continue
        cleaned = _scrub_webhook_event_payload(payload)
        await database.execute(
            "UPDATE pcs_shopify_webhook_events SET payload_json = CAST(:p AS jsonb) WHERE id = :id",
            {"p": json.dumps(cleaned, ensure_ascii=False, default=str), "id": row["id"]},
        )
        scrubbed += 1
    return scrubbed


async def _scrub_webhook_events_for_shop(*, shop_domain: Optional[str]) -> int:
    """Scrub PII from ALL order webhook-event rows for a shop. Returns count."""
    if not shop_domain:
        return 0
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT id, payload_json
        FROM pcs_shopify_webhook_events
        WHERE lower(shop_domain) = :shop_domain
          AND topic LIKE 'orders/%'
        """,
        {"shop_domain": shop_domain.strip().lower()},
    )
    scrubbed = 0
    for row in rows or []:
        payload = row["payload_json"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if not isinstance(payload, dict):
            continue
        cleaned = _scrub_webhook_event_payload(payload)
        await database.execute(
            "UPDATE pcs_shopify_webhook_events SET payload_json = CAST(:p AS jsonb) WHERE id = :id",
            {"p": json.dumps(cleaned, ensure_ascii=False, default=str), "id": row["id"]},
        )
        scrubbed += 1
    return scrubbed


async def _fulfill_shopify_gdpr_request(
    *,
    merchant_id: Optional[str],
    shop_domain: Optional[str],
    topic: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Fulfill a Shopify compliance obligation for real (not log-and-200).

    - customers/redact: anonymize matching Pivota `orders` rows and scrub matching
      pcs_shopify_webhook_events payloads.
    - shop/redact: same for the whole shop.
    - customers/data_request: export what Pivota holds for the customer into the
      audit row's resolution (merchant delivers out-of-band); no PII to app logs.

    Always records a shopify_gdpr_requests audit row. Wrapped so a failure marks
    the row needs_review; the caller still returns 200 (Shopify requires 200; the
    audit row is the source of truth).
    """
    from db.database import database

    resolution: Dict[str, Any] = {}
    status = "completed"

    # Compliance payloads carry ids, not bulk PII. Persist those for the audit row.
    customer = data.get("customer") if isinstance(data, dict) else None
    customer_email = None
    customer_id = None
    if isinstance(customer, dict):
        customer_email = (customer.get("email") or "").strip() or None
        customer_id = customer.get("id")
    orders_to_redact = data.get("orders_to_redact") if isinstance(data, dict) else None
    shopify_request = {
        "shop_domain": shop_domain,
        "customer_id": customer_id,
        "orders_to_redact": orders_to_redact,
        "orders_requested": data.get("orders_requested") if isinstance(data, dict) else None,
    }

    try:
        if topic == "customers/redact":
            redacted_orders = 0
            if merchant_id and customer_email:
                rows = await database.fetch_all(
                    """
                    UPDATE orders
                    SET customer_name = :redacted_name,
                        customer_email = :redacted_email,
                        shipping_address = NULL
                    WHERE merchant_id = :merchant_id
                      AND lower(customer_email) = :email
                    RETURNING order_id
                    """,
                    {
                        "redacted_name": _REDACTED_NAME,
                        "redacted_email": _redacted_email(customer_email),
                        "merchant_id": merchant_id,
                        "email": customer_email.strip().lower(),
                    },
                )
                redacted_orders = len(rows or [])
            # Also redact by explicit shopify order ids if provided.
            if merchant_id and isinstance(orders_to_redact, list) and orders_to_redact:
                ids = [str(x) for x in orders_to_redact if x is not None]
                if ids:
                    rows2 = await database.fetch_all(
                        """
                        UPDATE orders
                        SET customer_name = :redacted_name,
                            customer_email = :redacted_email,
                            shipping_address = NULL
                        WHERE merchant_id = :merchant_id
                          AND shopify_order_id = ANY(:ids)
                        RETURNING order_id
                        """,
                        {
                            "redacted_name": _REDACTED_NAME,
                            "redacted_email": _redacted_email(customer_email),
                            "merchant_id": merchant_id,
                            "ids": ids,
                        },
                    )
                    redacted_orders += len(rows2 or [])
            events_scrubbed = await _scrub_webhook_events_for_email(
                shop_domain=shop_domain, customer_email=customer_email
            )
            resolution = {"orders_redacted": redacted_orders, "webhook_events_scrubbed": events_scrubbed}

        elif topic == "shop/redact":
            orders_redacted = 0
            if merchant_id:
                rows = await database.fetch_all(
                    """
                    UPDATE orders
                    SET customer_name = :redacted_name,
                        customer_email = :redacted_email,
                        shipping_address = NULL
                    WHERE merchant_id = :merchant_id
                      AND customer_email NOT LIKE 'redacted+%@redacted.invalid'
                    RETURNING order_id
                    """,
                    {
                        "redacted_name": _REDACTED_NAME,
                        # Per-shop bulk redaction has no single original email; use a
                        # stable shop-scoped tombstone so the column stays non-null.
                        "redacted_email": _redacted_email(shop_domain),
                        "merchant_id": merchant_id,
                    },
                )
                orders_redacted = len(rows or [])
            events_scrubbed = await _scrub_webhook_events_for_shop(shop_domain=shop_domain)
            resolution = {"orders_redacted": orders_redacted, "webhook_events_scrubbed": events_scrubbed}

        elif topic == "customers/data_request":
            export: List[Dict[str, Any]] = []
            if merchant_id and customer_email:
                rows = await database.fetch_all(
                    """
                    SELECT order_id, shopify_order_id, created_at, total, currency, status, payment_status
                    FROM orders
                    WHERE merchant_id = :merchant_id
                      AND lower(customer_email) = :email
                    ORDER BY created_at DESC
                    """,
                    {"merchant_id": merchant_id, "email": customer_email.strip().lower()},
                )
                for r in rows or []:
                    d = dict(r)
                    export.append(
                        {
                            "order_id": d.get("order_id"),
                            "shopify_order_id": d.get("shopify_order_id"),
                            "created_at": str(d.get("created_at")) if d.get("created_at") else None,
                            "total": str(d.get("total")) if d.get("total") is not None else None,
                            "currency": d.get("currency"),
                            "status": d.get("status"),
                            "payment_status": d.get("payment_status"),
                        }
                    )
            # customer_email is an id in the compliance payload, not app-log PII, so
            # we do NOT log it; the export lives only in the audit row's resolution.
            resolution = {"orders_found": len(export), "export": export}

        else:
            status = "needs_review"
            resolution = {"error": f"unhandled_topic:{topic}"}

    except Exception as e:
        status = "needs_review"
        resolution = {"error": str(e)[:400]}
        logger.warning("GDPR fulfillment error topic=%s merchant=%s err=%s", topic, merchant_id, str(e)[:200])

    await _record_gdpr_request(
        merchant_id=merchant_id,
        shop_domain=shop_domain,
        topic=topic,
        shopify_request=shopify_request,
        status=status,
        resolution=resolution,
    )
    return {"status": status, "resolution": resolution}


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

    # Dual-app HMAC: verify against ALL app secrets (appstore + legacy + headless),
    # deduped, any-match. Once SHOPIFY_APPSTORE_CLIENT_SECRET diverges from the
    # legacy env, single-secret verification would 401 App A's compliance webhooks
    # and fail automated review (audit C2). Mirrors the main handler's candidate
    # pattern.
    secret_candidates = _shopify_app_secret_candidates()
    if not secret_candidates:
        record_shopify_webhook(result="error", reason="missing_secret", topic=topic)
        raise HTTPException(status_code=500, detail="Shopify webhook verification not configured")
    if not x_shopify_hmac_sha256:
        record_shopify_webhook(result="error", reason="missing_hmac", topic=topic)
        raise HTTPException(status_code=401, detail="Missing Shopify webhook signature")
    if not any(
        verify_shopify_hmac(secret=secret, payload=payload, header_hmac_base64=x_shopify_hmac_sha256)
        for secret in secret_candidates
    ):
        record_shopify_webhook(result="error", reason="invalid_signature", topic=topic)
        raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

    merchant_id = await _resolve_merchant_id_by_shop_domain(shop_domain)

    try:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    # Fulfill the obligation for real (redact / export) and persist the audit row.
    # Wrapped so Shopify still gets its required 200 even on error (the
    # shopify_gdpr_requests row records needs_review as the audit trail).
    outcome: Dict[str, Any] = {"status": "needs_review"}
    try:
        outcome = await _fulfill_shopify_gdpr_request(
            merchant_id=merchant_id,
            shop_domain=shop_domain,
            topic=topic,
            data=data,
        )
    except Exception as e:
        logger.warning("GDPR fulfillment top-level error topic=%s err=%s", topic, str(e)[:200])
        try:
            await _record_gdpr_request(
                merchant_id=merchant_id,
                shop_domain=shop_domain,
                topic=topic,
                shopify_request={"shop_domain": shop_domain},
                status="needs_review",
                resolution={"error": str(e)[:400]},
            )
        except Exception:
            pass

    # Preserve the existing order_events breadcrumb + metric (no PII: keys only).
    if merchant_id:
        try:
            await log_order_event(
                event_type="gdpr_webhook",
                order_id=f"gdpr_{merchant_id}",
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "payload_keys": list(data.keys()) if isinstance(data, dict) else [],
                    "shop_domain": shop_domain,
                    "resolution_status": outcome.get("status"),
                },
            )
        except Exception as e:
            logger.warning("GDPR webhook log failed merchant=%s err=%s", merchant_id, str(e)[:200])

    record_shopify_webhook(result="success", reason="ok", topic=topic)
    return {"status": "success", "topic": topic}


def _shopify_app_secret_candidates() -> list[str]:
    """
    Deduped list of app shared secrets that could sign a Shopify webhook in the
    dual-app (App A App-Store / App B headless) setup. Order is not significant;
    any-match acceptance mirrors the per-merchant handler.
    """
    candidates: list[str] = []
    for attr in ("shopify_appstore_client_secret", "shopify_client_secret", "shopify_headless_client_secret"):
        val = getattr(settings, attr, None)
        val = val.strip() if isinstance(val, str) else ""
        if val and val not in candidates:
            candidates.append(val)
    return candidates


async def _resolve_merchant_id_by_shop_domain(shop_domain: Optional[str]) -> Optional[str]:
    """
    Resolve merchant_id from a canonicalized Shopify shop domain, using the exact
    lookup the GDPR handler uses: merchant_stores by domain, then
    merchant_onboarding by mcp_shop_domain. Returns None if unknown.
    """
    if not shop_domain:
        return None
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
            return row["merchant_id"]
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
            return row["merchant_id"]
    except Exception as e:
        logger.warning("Shop-domain merchant lookup failed shop=%s err=%s", shop_domain, str(e)[:200])
    return None


@router.post("/shopify/orders")
async def handle_shopify_orders_static_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None),
    x_shopify_shop_domain: Optional[str] = Header(None),
    x_shopify_webhook_id: Optional[str] = Header(None),
    x_shopify_triggered_at: Optional[str] = Header(None),
):
    """
    Static order/uninstall webhook endpoint for the App Store app (App A).

    App A holds NO write_webhooks scope (deliberate) and therefore can never
    self-register per-merchant webhooks — its ONLY delivery mechanism is the
    app-owned shopify.app.toml `[[webhooks.subscriptions]]`, which deliver to ONE
    static uri. The per-merchant /webhooks/shopify/{merchant_id} address doesn't
    fit that model, so App A subscribes orders/paid + app/uninstalled here and we
    resolve the merchant from X-Shopify-Shop-Domain (audit fix #3).

    Strict in ALL environments (new endpoint, no legacy traffic): missing shop
    domain -> 401, missing/invalid HMAC -> 401, unknown shop -> 404. HMAC is
    verified against BOTH app secrets (App A + legacy) so a secret repoint does
    not silently 401 App A's traffic.
    """
    payload = await request.body()
    topic = x_shopify_topic or "unknown"

    if not x_shopify_shop_domain:
        record_shopify_webhook(result="error", reason="missing_shop_domain", topic=topic)
        raise HTTPException(status_code=401, detail="Missing Shopify shop domain")

    secret_candidates = _shopify_app_secret_candidates()
    if not secret_candidates:
        record_shopify_webhook(result="error", reason="missing_secret", topic=topic)
        raise HTTPException(status_code=500, detail="Shopify webhook verification not configured")
    if not x_shopify_hmac_sha256:
        record_shopify_webhook(result="error", reason="missing_hmac", topic=topic)
        raise HTTPException(status_code=401, detail="Missing Shopify webhook signature")

    signature_verified = any(
        verify_shopify_hmac(secret=secret, payload=payload, header_hmac_base64=x_shopify_hmac_sha256)
        for secret in secret_candidates
    )
    if not signature_verified:
        record_shopify_webhook(result="error", reason="invalid_signature", topic=topic)
        raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

    got_canon = _canonicalize_shop_domain(x_shopify_shop_domain)
    merchant_id = await _resolve_merchant_id_by_shop_domain(got_canon)
    if not merchant_id:
        # Unknown shop: 404 so Shopify stops retrying junk (and we emit a metric).
        record_shopify_webhook(result="error", reason="unknown_shop", topic=topic)
        logger.warning("Static Shopify order webhook: unknown shop domain got=%s topic=%s", got_canon, topic)
        raise HTTPException(status_code=404, detail="Unknown Shopify shop")

    try:
        data = json.loads(payload)
    except Exception:
        record_shopify_webhook(result="error", reason="invalid_json", topic=topic)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    shop_domain = x_shopify_shop_domain or "unknown"
    occurred_at: Optional[datetime] = None
    if x_shopify_triggered_at:
        try:
            occurred_at = datetime.fromisoformat(x_shopify_triggered_at.replace("Z", "+00:00"))
        except Exception:
            occurred_at = None

    # Identical post-verification processing as the per-merchant route.
    return await _process_shopify_webhook_event(
        merchant_id=merchant_id,
        payload=payload,
        data=data,
        topic=topic,
        shop_domain=shop_domain,
        got_canon=got_canon,
        occurred_at=occurred_at,
        x_shopify_webhook_id=x_shopify_webhook_id,
        background_tasks=background_tasks,
        signature_verified=signature_verified,
    )


# ============================================================================
# Shopify Webhooks
# ============================================================================


async def _process_shopify_webhook_event(
    *,
    merchant_id: str,
    payload: bytes,
    data: Any,
    topic: str,
    shop_domain: str,
    got_canon: Optional[str],
    occurred_at: Optional[datetime],
    x_shopify_webhook_id: Optional[str],
    background_tasks: BackgroundTasks,
    signature_verified: bool,
):
    """
    Shared post-verification processing for Shopify webhooks.

    Extracted verbatim from handle_shopify_webhook so BOTH the per-merchant
    route (POST /webhooks/shopify/{merchant_id}) and the static App-A order
    route (POST /webhooks/shopify/orders) execute IDENTICAL logic after HMAC
    verification + merchant resolution (audit fix #3). Callers own HMAC
    verification, shop-domain allowlisting, and merchant resolution; this helper
    owns idempotent ingest + topic dispatch. Zero behavior change vs. the prior
    inline body — parameters are passed exactly, especially `got_canon` for the
    ADR-009 seller-mismatch guard and the idempotency/duplicate return.
    """
    is_prod_runtime = _shopify_prod_runtime()
    try:
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
            if is_prod_runtime:
                record_shopify_webhook(result="error", reason="persist_failed", topic=topic)
                raise HTTPException(status_code=500, detail="Webhook event persistence unavailable")

        record_shopify_webhook(result="success", reason="ok", topic=topic)
        logger.info(f"Received Shopify webhook for {merchant_id}: {topic}")

        if topic.startswith("products/") or topic == "inventory_levels/update":
            catalog_event = await record_catalog_sync_event(
                merchant_id=merchant_id,
                connector="shopify",
                event_type="inventory_webhook" if topic == "inventory_levels/update" else "product_webhook",
                topic=topic,
                payload_json=data if isinstance(data, dict) else {"raw": data},
                source_ref=x_shopify_webhook_id or f"{merchant_id}:{topic}",
                occurred_at=occurred_at,
            )
            # ENQUEUE ONLY. Shopify gives a webhook ~5s and retries on
            # timeout, so the reconcile can never run inline here — but it must
            # not run in a `BackgroundTasks` task either: that task dies with
            # the process, and a dropped one left the merchant's catalog stale
            # AND the `catalog_sync_events` row stuck on `pending`, with a 200
            # already returned to Shopify so no retry was ever coming.
            #
            # Everything the reconcile needs is in `scope` now — `force_refresh`
            # re-pulls from Shopify before ingest (what the background task did
            # by hand) and `catalog_sync_event_id` lets the runner close the
            # event row. `services.catalog_sync_drain` picks it up.
            await create_catalog_sync_job(
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
                    "force_refresh": True,
                    "catalog_sync_event_id": str(catalog_event.get("event_id") or ""),
                },
                requested_by="shopify_webhook",
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
                # Public recall gates on catalog_merchants.status, which nothing
                # used to write — so an uninstalled merchant kept serving on
                # search indefinitely (#1648). Re-derive it from the stores we
                # just changed. Never raises.
                from services.store_lifecycle_service import sync_catalog_merchant_status

                await sync_catalog_merchant_status(merchant_id, reason="shopify_app_uninstalled")
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

            # T2-2: close the attribution loop for a purchase completed on an
            # un-integrated merchant's OWN Shopify checkout. There is no Pivota
            # `orders` row (result is None above) — recovery goes through the
            # `pivota_click_id` T2-1 stamped onto the cart permalink, which
            # Shopify persisted into note_attributes. Gated on `orders/paid`
            # (the conversion event; orders/create precedes payment). This runs
            # only AFTER the HMAC verification above (lines ~1742-1749) — the
            # same signature gate the whole handler is behind. Best-effort and
            # fully isolated: a failure here must not break order-event logging
            # or change the webhook's 200 for an otherwise-valid order.
            if topic == "orders/paid":
                try:
                    click_id = extract_click_id_from_note_attributes(
                        data.get("note_attributes")
                    )
                    if click_id:
                        amount_cents, order_currency = shopify_order_total_to_cents(data)
                        await close_external_order_conversion(
                            merchant_id=merchant_id,
                            click_id=click_id,
                            external_order_id=shopify_order_id,
                            gross_amount_cents=amount_cents,
                            currency=order_currency,
                            converted_at=occurred_at,
                            note_attrs_or_payload=data if isinstance(data, dict) else None,
                            # ADR-009 §D3 seller-mismatch guard: pass the SAME shop
                            # domain this handler authenticated. `got_canon` is the
                            # canonicalized `X-Shopify-Shop-Domain`; in production it
                            # was verified to be a connected store for this merchant
                            # and to key the HMAC secret (see the shop-domain-mismatch
                            # + secret-resolution gate above), so it is the store the
                            # sale actually happened on. Best-effort: it can be None
                            # off-production (missing header) → the closure stamps
                            # `seller_domain_unverified` rather than crashing.
                            converting_shop_domain=got_canon,
                        )
                except Exception as e:
                    logger.warning(
                        "T2-2 external conversion close failed merchant=%s shopify_order=%s err=%s",
                        merchant_id,
                        shopify_order_id,
                        str(e)[:200],
                    )

        elif topic == "orders/cancelled":
            # 订单取消
            shopify_order_id = str(data.get("id"))
            cancel_reason = data.get("cancel_reason")

            query = "SELECT * FROM orders WHERE shopify_order_id = :shopify_order_id"
            from db.database import database
            result = await database.fetch_one(query, {"shopify_order_id": shopify_order_id})

            if result:
                result = _db_row_to_dict(result)
                order_id = result["order_id"]
                existing_payment_status = str(result.get("payment_status") or "").strip().lower()
                existing_status = str(result.get("status") or "").strip().lower()
                # Guard: do NOT blindly cancel a paid/shipped/fulfilled/refunded
                # order on a Shopify orders/cancelled webhook. Cancelling a paid
                # order here would silently strand a real charge. Only cancel
                # orders that have not yet reached a paid/terminal state.
                protected = (
                    existing_payment_status in {"paid", "completed", "succeeded", "settled", "refunded", "partially_refunded"}
                    or existing_status in {"paid", "completed", "fulfilled", "shipped", "refunded", "partially_refunded", "cancelled"}
                )
                if protected:
                    logger.warning(
                        "Shopify orders/cancelled webhook ignored for order %s in protected state "
                        "(payment_status=%s status=%s); not auto-cancelling a settled order",
                        order_id,
                        existing_payment_status,
                        existing_status,
                    )
                    await log_order_event(
                        event_type="order_cancel_webhook_ignored_protected_state",
                        order_id=order_id,
                        merchant_id=merchant_id,
                        metadata={
                            "shopify_order_id": shopify_order_id,
                            "cancel_reason": cancel_reason,
                            "payment_status": existing_payment_status,
                            "status": existing_status,
                        },
                    )
                else:
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
            # Fulfill the obligation for real (redact / export) + audit row, same
            # shared handler as the static /gdpr endpoint. got_canon is the
            # canonicalized shop domain this handler authenticated. Wrapped so a
            # failure marks needs_review and still returns 200.
            outcome: Dict[str, Any] = {"status": "needs_review"}
            try:
                outcome = await _fulfill_shopify_gdpr_request(
                    merchant_id=merchant_id,
                    shop_domain=got_canon,
                    topic=topic,
                    data=data if isinstance(data, dict) else {},
                )
            except Exception as e:
                logger.warning("GDPR fulfillment (per-merchant) error topic=%s err=%s", topic, str(e)[:200])
            await log_order_event(
                event_type="gdpr_webhook",
                order_id=f"gdpr_{merchant_id}",
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "payload_keys": list((data or {}).keys()),
                    "resolution_status": outcome.get("status"),
                },
            )

        return {"status": "success", "topic": topic}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error handling Shopify webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        is_prod_runtime = _shopify_prod_runtime()
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
        if is_prod_runtime:
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
        if is_prod_runtime and not signature_verified:
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

        # Delegate all post-verification processing (idempotent ingest +
        # topic dispatch) to the shared helper. See _process_shopify_webhook_event.
        return await _process_shopify_webhook_event(
            merchant_id=merchant_id,
            payload=payload,
            data=data,
            topic=topic,
            shop_domain=shop_domain,
            got_canon=got_canon,
            occurred_at=occurred_at,
            x_shopify_webhook_id=x_shopify_webhook_id,
            background_tasks=background_tasks,
            signature_verified=signature_verified,
        )
        
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
    callback_base_url: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    为商户注册 Shopify webhooks

    SECURITY: employee-authenticated only. This endpoint uses the merchant's
    stored admin token to point that store's order webhooks (full-PII payloads)
    at a caller-supplied callback_base_url — an unauthenticated caller could
    re-point a store's webhooks to an attacker host. Gated to ops/employee
    credentials, matching the equivalent ops route in
    routes/ops_shopify_integration_routes.py. See audit fix #2.

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
            # Catalog and inventory cache/index updates
            "products/create",
            "products/update",
            "products/delete",
            "inventory_levels/update",
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
            # NOTE: compliance topics (customers/data_request, customers/redact, shop/redact)
            # are toml/dashboard-managed app-config subscriptions and CANNOT be REST-registered.
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
                
                url = f"https://{shop_domain_canon}/admin/api/2025-10/webhooks.json"
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
