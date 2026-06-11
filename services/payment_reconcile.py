"""
Pending-payment reconcile sweep.

Safety net behind the Stripe webhook. Hosted-checkout orders finalize to `paid`
via the `payment_intent.succeeded` webhook; if that single delivery never matches
(orphaned metadata.order_id, cs_-vs-pi_ correlation gap, order committed after the
event arrived, or a dropped webhook), a real charge can succeed while the order
stays `awaiting_payment` forever — the charge-stuck incident.

This sweep periodically re-checks orders that are still pending but already carry a
PSP payment reference, asks the PSP for the authoritative status (reusing
`verify_order_payment_succeeded`, which also enforces amount + currency match), and
finalizes the ones that actually succeeded — driving them to `paid` + Shopify
fulfillment exactly as the webhook would have.

It is deliberately conservative:
- only touches orders with a non-empty payment reference,
- skips very recent orders (a confirm may still be in flight),
- skips ancient orders (don't reanimate long-abandoned drafts),
- bounded per tick, single-instance via the scheduler,
- relies on the same atomic `mark_order_paid` transition as the webhook, so it can
  never double-fulfill an order the webhook already settled.
"""

import os
from typing import Any, Dict, Optional

from utils.logger import logger


def _reconcile_sweep_enabled() -> bool:
    """The sweep auto-finalizes payments + creates merchant orders, so it is
    OFF by default and must be deliberately enabled for controlled rollout.
    Important under single-DB tenancy (staging shares the prod Postgres): a
    staging deploy must not silently reconcile prod orders."""
    return str(os.getenv("PAYMENT_RECONCILE_SWEEP_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

# How long after creation before we consider an order "stuck" (give the
# synchronous confirm / webhook a chance first).
_MIN_AGE_SECONDS = 120
# Don't reconcile orders older than this (abandoned drafts, not stuck charges).
_MAX_AGE_HOURS = 72
# Max orders to reconcile per tick (bounded DB + PSP load).
_MAX_ORDERS_PER_TICK = 50

_PENDING_PAYMENT_STATUSES = ("awaiting_payment", "processing", "requires_action")


async def reconcile_pending_payments(
    *,
    max_orders: int = _MAX_ORDERS_PER_TICK,
    min_age_seconds: int = _MIN_AGE_SECONDS,
    max_age_hours: int = _MAX_AGE_HOURS,
) -> Dict[str, Any]:
    """Reconcile stuck pending-payment orders against the PSP. Best-effort."""
    from db.database import database

    summary: Dict[str, Any] = {
        "scanned": 0,
        "finalized": 0,
        "still_pending": 0,
        "errors": 0,
    }

    try:
        rows = await database.fetch_all(
            f"""
            SELECT *
            FROM orders
            WHERE COALESCE(LOWER(payment_status), '') IN (
                'awaiting_payment', 'processing', 'requires_action'
            )
              AND payment_intent_id IS NOT NULL
              AND payment_intent_id <> ''
              AND created_at < (NOW() - (:min_age_seconds * INTERVAL '1 second'))
              AND created_at > (NOW() - (:max_age_hours * INTERVAL '1 hour'))
            ORDER BY created_at DESC
            LIMIT :max_orders
            """,
            {
                "min_age_seconds": int(min_age_seconds),
                "max_age_hours": int(max_age_hours),
                "max_orders": int(max_orders),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment_reconcile: failed to load pending orders: %s", exc)
        return summary

    if not rows:
        return summary

    # Lazy imports to avoid import cycles (order_routes imports widely).
    from db.orders import mark_order_paid, update_payment_info
    from db.products import log_order_event
    from routes.order_routes import (
        create_shopify_order,
        verify_order_payment_succeeded,
    )
    from services.merchant_store_service import get_primary_store
    from services.psp_payment_finalizer import finalize_payment_success

    summary["scanned"] = len(rows)

    for raw in rows:
        order = dict(raw)
        order_id = str(order.get("order_id") or "")
        merchant_id = str(order.get("merchant_id") or "")
        try:
            ok, psp_status, error = await verify_order_payment_succeeded(order)
            if not ok:
                summary["still_pending"] += 1
                logger.info(
                    "payment_reconcile: order %s still pending (psp_status=%s err=%s)",
                    order_id,
                    psp_status,
                    error,
                )
                continue

            finalization = await finalize_payment_success(
                order,
                psp=str(order.get("psp_used") or "stripe"),
                payment_reference=str(order.get("payment_intent_id") or ""),
                currency=str(order.get("currency") or ""),
                source_event="payment_reconcile_sweep",
                update_payment_info_fn=update_payment_info,
                mark_order_paid_fn=mark_order_paid,
                log_order_event_fn=log_order_event,
            )

            if not finalization.get("transitioned"):
                # Webhook/sync confirm already settled it between our SELECT and
                # finalize — nothing to do.
                logger.info(
                    "payment_reconcile: order %s already settled concurrently", order_id
                )
                continue

            summary["finalized"] += 1
            logger.warning(
                "payment_reconcile: recovered stuck-paid order %s (psp_status=%s) — "
                "webhook did not finalize it",
                order_id,
                psp_status,
            )

            # Mirror the webhook's fulfillment side effect.
            order_metadata = order.get("metadata") or {}
            skip_platform_order_creation = isinstance(order_metadata, dict) and (
                str(order_metadata.get("skip_platform_order_creation") or "").strip().lower()
                in {"1", "true", "yes", "on"}
                or str(order_metadata.get("ops_canary") or "").strip().lower()
                in {"1", "true", "yes", "on"}
            )
            if not skip_platform_order_creation and merchant_id:
                try:
                    store_info = await get_primary_store(merchant_id)
                    if store_info and store_info.get("platform") == "shopify":
                        await create_shopify_order(order_id)
                except Exception as shop_err:  # noqa: BLE001
                    logger.error(
                        "payment_reconcile: Shopify order creation failed for %s: %s",
                        order_id,
                        shop_err,
                    )
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            logger.warning(
                "payment_reconcile: error reconciling order %s: %s", order_id, exc
            )

    if summary["finalized"] or summary["errors"]:
        logger.info("payment_reconcile tick summary: %s", summary)
    return summary


async def run_payment_reconcile_tick() -> None:
    """Scheduler entrypoint (best-effort; never raises). Dormant unless
    PAYMENT_RECONCILE_SWEEP_ENABLED is set."""
    if not _reconcile_sweep_enabled():
        return
    try:
        await reconcile_pending_payments()
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment_reconcile: tick failed: %s", exc)
