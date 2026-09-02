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
    from routes.order_routes import create_shopify_order

    cutoff = datetime.utcnow() - timedelta(seconds=int(min_age_seconds))
    merchant_clause = "AND merchant_id = :merchant_id" if merchant_id else ""
    # Bind ONLY what `merchant_clause` actually interpolates. `databases` hands
    # this dict straight to text().bindparams(), which raises ArgumentError for
    # a parameter the query never declared — and the scheduled caller always
    # passes merchant_id=None, so an unconditional bind fails every real run.
    values: Dict[str, Any] = {"cutoff": cutoff, "limit": int(limit)}
    if merchant_id:
        values["merchant_id"] = merchant_id
    rows = await database.fetch_all(
        f"""
        SELECT order_id
        FROM orders
        WHERE is_deleted = false
          AND payment_status = 'paid'
          AND (shopify_order_id IS NULL OR shopify_order_id = '')
          AND (
            (paid_at IS NOT NULL AND paid_at <= :cutoff)
            OR (paid_at IS NULL AND created_at <= :cutoff)
          )
          {merchant_clause}
        ORDER BY created_at ASC
        LIMIT :limit
        """,
        values,
    )
    order_ids = [str(dict(row).get("order_id") or "") for row in rows or []]
    order_ids = [order_id for order_id in order_ids if order_id]
    if dry_run:
        return {"dry_run": True, "candidates": order_ids, "count": len(order_ids)}

    succeeded: List[str] = []
    failed: List[Dict[str, str]] = []
    for order_id in order_ids:
        try:
            ok = await create_shopify_order(order_id)
            if ok:
                succeeded.append(order_id)
            else:
                failed.append({"order_id": order_id, "error": "create_shopify_order returned false"})
        except Exception as exc:
            failed.append({"order_id": order_id, "error": f"{type(exc).__name__}: {str(exc)}"})

    return {
        "dry_run": False,
        "attempted": len(order_ids),
        "succeeded": len(succeeded),
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
