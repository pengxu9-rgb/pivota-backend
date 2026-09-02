"""
Cron-compatible agentic commerce reconciliation.

Typical usage:

    python -m jobs.agentic_commerce_reconciliation --limit-merchants 50

Schedule this command from cron, Railway, GitHub Actions, or another scheduler.
The default cadence target is controlled by AGENTIC_COMMERCE_RECONCILIATION_CADENCE_MINUTES.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from db.database import database
from services.catalog_sync_service import create_catalog_sync_job, run_catalog_sync_job
from services.shopify_products_sync import sync_shopify_products_for_merchant

logger = logging.getLogger(__name__)


async def _active_shopify_merchant_ids(limit: int) -> List[str]:
    rows = await database.fetch_all(
        """
        SELECT DISTINCT merchant_id
        FROM merchant_stores
        WHERE platform = 'shopify'
          AND status IN ('active', 'connected')
        ORDER BY merchant_id
        LIMIT :limit
        """,
        {"limit": int(limit)},
    )
    merchant_ids: List[str] = []
    for row in rows or []:
        try:
            value = row["merchant_id"]
        except Exception:
            value = dict(row).get("merchant_id") if row else None
        if value:
            merchant_ids.append(str(value))
    return merchant_ids


async def reconcile_shopify_catalog_for_merchant(
    merchant_id: str,
    *,
    limit_products: int,
    force_refresh: bool,
) -> Dict[str, Any]:
    refresh_summary: Optional[Dict[str, Any]] = None
    if force_refresh:
        refresh_summary = await sync_shopify_products_for_merchant(
            merchant_id=merchant_id,
            limit=limit_products,
            ingest_catalog=False,
        )

    job = await create_catalog_sync_job(
        merchant_id=merchant_id,
        connector="shopify",
        mode="reconcile",
        scope={
            "platform": "shopify",
            "limit": int(limit_products),
            "include_expired": True,
            "source_system": "products_cache",
            "force_refresh": bool(force_refresh),
            "scheduled": True,
        },
        requested_by="agentic-commerce-reconciliation",
    )
    completed = await run_catalog_sync_job(str(job.get("job_id") or ""))
    return {
        "merchant_id": merchant_id,
        "job_id": str(job.get("job_id") or ""),
        "status": completed.get("status"),
        "stats": completed.get("stats_json") or completed.get("stats") or {},
        "refresh": refresh_summary,
    }


async def reconcile_paid_orders_missing_merchant_order(
    *,
    merchant_id: Optional[str],
    limit: int,
    min_age_seconds: int,
    dry_run: bool,
) -> Dict[str, Any]:
    """Repair orders whose merchant-order create was never QUEUED.

    This used to call `create_shopify_order` directly on every candidate. That
    was safe only because it ran by hand: on a schedule it re-POSTs, and the
    create is not remotely idempotent on WooCommerce, Wix or BigCommerce — none
    of them looks for an existing order first. An order whose create partially
    landed (a real Wix order written, the link write timing out) matches this
    query forever, so every tick would have made another merchant order and
    another shipment for the same buyer.

    So it now ENQUEUES onto the durable queue instead of creating, and only for
    orders that have no create job at all. That confines it to exactly the gap
    it exists for — an enqueue that was lost — and inherits the queue's
    at-most-once guarantee.

    An order whose job already ran is not re-attempted here, whatever the
    outcome — and note that includes a job marked `done` on the contended-lock
    path, where the handler returns `create_in_progress_elsewhere` having
    created nothing. If that lock holder then died, the order sits outside this
    lane permanently. It stays visible through
    `paid_missing_merchant_order_count`, and the ops retry endpoint repairs it
    by calling `sync_order_to_connected_store` directly. A `failed` job is the
    same story. Neither is re-attempted automatically, because retrying a
    create on a platform that cannot dedupe it is the duplicate-order problem.
    """
    from db.merchant_order_sync_jobs import (
        OP_MERCHANT_ORDER_CREATE,
        MERCHANT_ORDER_CREATE_DEDUPE_KEY,
        ensure_merchant_order_sync_jobs_table,
        enqueue_merchant_order_create,
    )

    await ensure_merchant_order_sync_jobs_table()

    cutoff = datetime.utcnow() - timedelta(seconds=int(min_age_seconds))
    merchant_clause = "AND merchant_id = :merchant_id" if merchant_id else ""
    # Bind ONLY what `merchant_clause` actually interpolates. `databases` hands
    # this dict straight to text().bindparams(), which raises ArgumentError for
    # a parameter the query never declared — and the scheduled caller always
    # passes merchant_id=None, so an unconditional bind fails every real run.
    values: Dict[str, Any] = {
        "cutoff": cutoff,
        "limit": int(limit),
        "op": OP_MERCHANT_ORDER_CREATE,
        "dedupe_key": MERCHANT_ORDER_CREATE_DEDUPE_KEY,
    }
    if merchant_id:
        values["merchant_id"] = merchant_id
    rows = await database.fetch_all(
        f"""
        SELECT order_id, merchant_id
        FROM orders
        WHERE COALESCE(is_deleted, false) = false
          AND payment_status = 'paid'
          AND (shopify_order_id IS NULL OR shopify_order_id = '')
          -- A WooCommerce/Wix/BigCommerce order records its id in metadata and
          -- leaves shopify_order_id empty, so without this every successfully
          -- delivered non-Shopify order matched this query forever.
          AND COALESCE(metadata -> 'merchant_order' ->> 'platform_order_id', '') = ''
          AND (
            (paid_at IS NOT NULL AND paid_at <= :cutoff)
            OR (paid_at IS NULL AND created_at <= :cutoff)
          )
          -- Only orders the queue has never been told about. A job in ANY state
          -- means the queue owns this order; re-enqueuing would revive it for
          -- another attempt on every tick, which is the amplification this
          -- lane must not reintroduce.
          AND NOT EXISTS (
            SELECT 1 FROM merchant_order_sync_jobs j
             WHERE j.order_id = orders.order_id
               AND j.op = :op
               AND j.dedupe_key = :dedupe_key
          )
          {merchant_clause}
        ORDER BY created_at ASC
        LIMIT :limit
        """,
        values,
    )
    candidates = [
        (str(d.get("order_id") or ""), str(d.get("merchant_id") or ""))
        for d in (dict(r) for r in rows or [])
    ]
    candidates = [c for c in candidates if c[0]]
    order_ids = [c[0] for c in candidates]
    if dry_run:
        return {"dry_run": True, "candidates": order_ids, "count": len(order_ids)}

    queued: List[str] = []
    failed: List[Dict[str, str]] = []
    for order_id, order_merchant_id in candidates:
        try:
            job_id = await enqueue_merchant_order_create(
                order_id=order_id, merchant_id=order_merchant_id
            )
            if job_id:
                queued.append(order_id)
                logger.warning(
                    "agentic_commerce_reconciliation: order %s was paid with no "
                    "merchant order and no create job — the enqueue was lost; "
                    "queued as %s",
                    order_id,
                    job_id,
                )
            else:
                failed.append({"order_id": order_id, "error": "enqueue returned None"})
        except Exception as exc:
            failed.append({"order_id": order_id, "error": f"{type(exc).__name__}: {str(exc)}"})

    return {
        "dry_run": False,
        "attempted": len(order_ids),
        "queued": len(queued),
        "failed": len(failed),
        "failed_orders": failed[:50],
    }


async def run_agentic_commerce_reconciliation_once(
    *,
    limit_merchants: int = 50,
    limit_products: int = 500,
    force_refresh: bool = True,
    order_limit: int = 50,
    order_min_age_seconds: int = 120,
    dry_run: bool = False,
) -> Dict[str, Any]:
    merchant_ids = await _active_shopify_merchant_ids(limit_merchants)
    catalog_results: List[Dict[str, Any]] = []
    catalog_failures: List[Dict[str, str]] = []

    for merchant_id in merchant_ids:
        try:
            if dry_run:
                catalog_results.append({"merchant_id": merchant_id, "dry_run": True})
            else:
                catalog_results.append(
                    await reconcile_shopify_catalog_for_merchant(
                        merchant_id,
                        limit_products=limit_products,
                        force_refresh=force_refresh,
                    )
                )
        except Exception as exc:
            logger.exception("Scheduled Shopify catalog reconciliation failed merchant_id=%s", merchant_id)
            catalog_failures.append({"merchant_id": merchant_id, "error": f"{type(exc).__name__}: {str(exc)}"})

    order_result = await reconcile_paid_orders_missing_merchant_order(
        merchant_id=None,
        limit=order_limit,
        min_age_seconds=order_min_age_seconds,
        dry_run=dry_run,
    )

    return {
        "status": "success" if not catalog_failures and not order_result.get("failed") else "partial",
        "cadence_minutes": int(os.getenv("AGENTIC_COMMERCE_RECONCILIATION_CADENCE_MINUTES", "60") or "60"),
        "shopify_merchants": len(merchant_ids),
        "catalog_results": catalog_results,
        "catalog_failures": catalog_failures,
        "missing_merchant_orders": order_result,
    }


async def _main_async(args: argparse.Namespace) -> None:
    await database.connect()
    try:
        result = await run_agentic_commerce_reconciliation_once(
            limit_merchants=args.limit_merchants,
            limit_products=args.limit_products,
            force_refresh=not args.no_force_refresh,
            order_limit=args.order_limit,
            order_min_age_seconds=args.order_min_age_seconds,
            dry_run=args.dry_run,
        )
        logger.info("Agentic commerce reconciliation result: %s", result)
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scheduled agentic commerce reconciliation once.")
    parser.add_argument("--limit-merchants", type=int, default=50)
    parser.add_argument("--limit-products", type=int, default=500)
    parser.add_argument("--order-limit", type=int, default=50)
    parser.add_argument("--order-min-age-seconds", type=int, default=120)
    parser.add_argument("--no-force-refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()


def _create_reconcile_enabled() -> bool:
    """OFF by default. This lane enqueues merchant-order creates on the money
    path, staging shares the prod Postgres, and its first prod run will pick up
    every pre-queue order that never got one — so it is armed deliberately,
    after a `--dry-run` has sized that backlog."""
    return str(
        os.getenv("MERCHANT_ORDER_CREATE_RECONCILE_ENABLED", "")
    ).strip().lower() in {"1", "true", "yes", "on"}


async def run_merchant_order_create_reconcile_tick() -> Dict[str, Any]:
    """Scheduler entrypoint. Best-effort; never raises."""
    if not _create_reconcile_enabled():
        return {"status": "disabled"}
    try:
        return await reconcile_paid_orders_missing_merchant_order(
            merchant_id=None,
            limit=50,
            min_age_seconds=600,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "merchant_order_create_reconcile: tick failed: %s", str(exc)[:300]
        )
        return {"status": "error", "error": str(exc)[:300]}
