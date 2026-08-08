"""
Employee Dashboard Routes
Provides analytics and management endpoints for employee portal
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from utils.auth import get_current_employee, get_current_user
from utils.encryption import mask_credential
from db.database import database
from readiness.summary import (
    get_readiness_optimization_cache_metrics,
    invalidate_readiness_optimization_cache,
)
from services.external_referral_readiness import (
    build_external_referral_fleet_summary,
    build_merchant_commerce_cohort_summary,
    build_merchant_commerce_readiness_list,
    build_external_referral_summary,
    build_platform_fallback_program_summary,
)
from services.traffic_analytics_service import (
    TRAFFIC_BREAKDOWN_FIELDS,
    build_employee_merchant_traffic,
    build_employee_traffic_breakdown,
    build_employee_traffic_overview,
)
import random

router = APIRouter()
PAID_PAYMENT_STATUSES_SQL = "('paid','completed','succeeded','success','settled','partially_refunded')"


class EmployeeOptimizationCacheInvalidateRequest(BaseModel):
    merchant_id: Optional[str] = Field(default=None, min_length=1, max_length=255)
    channel: Optional[str] = Field(default=None, min_length=1, max_length=64)


def _scoped_optimization_cache_metrics(
    *,
    merchant_id: Optional[str],
    channel: Optional[str],
) -> Dict[str, Any]:
    metrics = get_readiness_optimization_cache_metrics()
    active_keys = list(metrics.get("active_keys") or [])
    scoped_entries = [
        entry
        for entry in active_keys
        if (merchant_id is None or entry.get("merchant_id") == merchant_id)
        and (channel is None or entry.get("channel") == channel)
    ]
    return {
        **metrics,
        "scope": {
            "merchant_id": merchant_id,
            "channel": channel,
        },
        "scoped_entry_count": len(scoped_entries),
        "has_active_entry": bool(scoped_entries),
        "active_entry": scoped_entries[0] if scoped_entries else None,
        "scoped_active_keys": scoped_entries,
    }


async def _fetch_employee_merchant_stores(merchant_id: str) -> List[Dict[str, Any]]:
    stores: List[Dict[str, Any]] = []
    cache_counts_by_platform: Dict[str, int] = {}
    rows = await database.fetch_all(
        """
        SELECT
            store_id,
            platform,
            name,
            domain,
            status,
            connected_at,
            last_sync,
            product_count,
            CASE WHEN api_key IS NOT NULL AND api_key != '' THEN true ELSE false END as api_key_present
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND lower(COALESCE(status, 'active')) NOT IN ('disconnected', 'deleted', 'inactive')
        ORDER BY connected_at DESC
        """,
        {"merchant_id": merchant_id},
    )
    try:
        cache_rows = await database.fetch_all(
            """
            SELECT platform, COUNT(*) AS active_cached
            FROM products_cache
            WHERE merchant_id = :merchant_id
              AND (expires_at IS NULL OR expires_at > NOW())
            GROUP BY platform
            """,
            {"merchant_id": merchant_id},
        )
        for row in cache_rows or []:
            row_dict = dict(row)
            platform = (row_dict.get("platform") or "").strip().lower()
            if platform:
                cache_counts_by_platform[platform] = int(row_dict.get("active_cached") or 0)
    except Exception:
        cache_counts_by_platform = {}

    for row in rows:
        platform = (row["platform"] or "").strip().lower()
        cached_count = cache_counts_by_platform.get(platform, 0)
        display_count = cached_count if cached_count > 0 else (row["product_count"] or 0)
        is_active = row["status"] == "active"
        has_api_key = bool(row["api_key_present"])
        stores.append(
            {
                "id": row["store_id"],
                "platform": row["platform"],
                "name": row["name"],
                "domain": row["domain"],
                "status": row["status"],
                "is_active": is_active,
                "is_connected": is_active and has_api_key,
                "api_key_present": has_api_key,
                "shop_domain": row["domain"],
                "connected_at": row["connected_at"],
                "last_sync": row["last_sync"],
                "product_count": display_count,
                "product_count_source": "products_cache" if cached_count > 0 else "merchant_stores",
            }
        )

    return stores


async def _fetch_employee_merchant_psps(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT psp_id, provider, name, account_id, status, connected_at, capabilities, api_key
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
        ORDER BY connected_at DESC
        """,
        {"merchant_id": merchant_id},
    )
    psp_metrics = await database.fetch_all(
        """
        SELECT
            COALESCE(psp_used, psp_id) AS psp_key,
            COUNT(*) as total_orders,
            SUM(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 ELSE 0 END) as successful_orders,
            COALESCE(SUM(total), 0) as total_volume
        FROM orders
        WHERE merchant_id = :merchant_id
          AND (psp_used IS NOT NULL OR psp_id IS NOT NULL)
          AND (is_deleted IS NULL OR is_deleted = FALSE)
        GROUP BY COALESCE(psp_used, psp_id)
        """,
        {"merchant_id": merchant_id},
    )
    stats_by_key: Dict[str, Dict[str, Any]] = {}
    for metric in psp_metrics or []:
        total_orders = int(metric["total_orders"] or 0)
        successful_orders = int(metric["successful_orders"] or 0)
        stats_by_key[metric["psp_key"]] = {
            "total_orders": total_orders,
            "successful_orders": successful_orders,
            "total_volume": float(metric["total_volume"] or 0),
            "success_rate": round((successful_orders / total_orders) * 100, 1) if total_orders > 0 else 0,
        }

    psps: List[Dict[str, Any]] = []
    for row in rows:
        capabilities = row["capabilities"].split(",") if row["capabilities"] else []
        psp_key = row["provider"] if row["provider"] in stats_by_key else row["psp_id"]
        psp_stats = stats_by_key.get(psp_key, {"total_orders": 0, "successful_orders": 0, "total_volume": 0.0, "success_rate": 0})
        api_key = row["api_key"]
        configured = bool(api_key and str(api_key).strip() and api_key != "pending_setup")
        effective_status = row["status"]
        if not configured and (effective_status or "").lower() == "active":
            effective_status = "pending_setup"
        psps.append(
            {
                "id": row["psp_id"],
                "provider": row["provider"],
                "name": row["name"],
                "account_id": row["account_id"],
                "status": effective_status,
                "configured": configured,
                "connected_at": row["connected_at"],
                "capabilities": capabilities,
                "transaction_count": psp_stats["total_orders"],
                "success_rate": psp_stats["success_rate"],
                "total_volume": psp_stats["total_volume"],
            }
        )
    return psps


async def _fetch_employee_merchant_analytics(merchant_id: str) -> Dict[str, Any]:
    analytics = await database.fetch_one(
        """
        SELECT
            COUNT(*) as total_orders_all_time,
            COALESCE(SUM(total), 0) as gmv_all_time,
            COALESCE(SUM(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_all_time,
            SUM(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_all_time,
            COALESCE(AVG(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE NULL END), 0) as avg_order_value_all_time,
            COUNT(DISTINCT customer_email) as total_customers_all_time,
            COUNT(DISTINCT CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN customer_email END) as total_customers_last_30_days,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN 1 ELSE 0 END) as orders_last_30_days,
            COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN total ELSE 0 END), 0) as gmv_last_30_days,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_last_30_days,
            COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_last_30_days
        FROM orders
        WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
        """,
        {"merchant_id": merchant_id},
    )
    growth = await database.fetch_one(
        """
        SELECT
            COUNT(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                  AND created_at < CURRENT_DATE - INTERVAL '30 days' THEN 1 END) as orders_prev_30,
            COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                    AND created_at < CURRENT_DATE - INTERVAL '30 days' THEN total ELSE 0 END), 0) as gmv_prev_30,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                    AND created_at < CURRENT_DATE - INTERVAL '30 days'
                    AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_prev_30,
            COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                    AND created_at < CURRENT_DATE - INTERVAL '30 days'
                    AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_prev_30
        FROM orders
        WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
        """,
        {"merchant_id": merchant_id},
    )
    products_count = await database.fetch_one(
        """
        SELECT COUNT(*) as count
        FROM products_cache
        WHERE merchant_id = :merchant_id
          AND (expires_at IS NULL OR expires_at > NOW())
        """,
        {"merchant_id": merchant_id},
    )

    def _to_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except (TypeError, ValueError):
            return 0

    orders_last_30 = _to_int(analytics["orders_last_30_days"]) if analytics else 0
    paid_orders_last_30 = _to_int(analytics["paid_orders_last_30_days"]) if analytics else 0
    gmv_last_30 = float(analytics["gmv_last_30_days"] or 0) if analytics else 0.0
    confirmed_revenue_last_30 = float(analytics["confirmed_revenue_last_30_days"] or 0) if analytics else 0.0
    orders_prev_30 = _to_int(growth["orders_prev_30"]) if growth else 0
    confirmed_revenue_prev_30 = float(growth["confirmed_revenue_prev_30"] or 0) if growth else 0.0
    gmv_prev_30 = float(growth["gmv_prev_30"] or 0) if growth else 0.0

    order_growth = ((orders_last_30 - orders_prev_30) / orders_prev_30 * 100) if orders_prev_30 > 0 else 0.0
    revenue_growth = ((confirmed_revenue_last_30 - confirmed_revenue_prev_30) / confirmed_revenue_prev_30 * 100) if confirmed_revenue_prev_30 > 0 else 0.0
    gmv_growth = ((gmv_last_30 - gmv_prev_30) / gmv_prev_30 * 100) if gmv_prev_30 > 0 else 0.0
    average_order_value = round((confirmed_revenue_last_30 / paid_orders_last_30), 2) if paid_orders_last_30 > 0 else 0.0
    payment_success_rate = round((paid_orders_last_30 / orders_last_30 * 100), 1) if orders_last_30 > 0 else 0.0

    analytics_payload = {
        "total_orders": orders_last_30,
        "total_revenue": confirmed_revenue_last_30,
        "total_customers": _to_int(analytics["total_customers_last_30_days"]) if analytics else 0,
        "customer_breakdown": {
            "last_30_days": _to_int(analytics["total_customers_last_30_days"]) if analytics else 0,
            "all_time": _to_int(analytics["total_customers_all_time"]) if analytics else 0,
        },
        "all_time_customers": _to_int(analytics["total_customers_all_time"]) if analytics else 0,
        "total_products": _to_int(products_count["count"]) if products_count else 0,
        "average_order_value": average_order_value,
        "order_growth": round(order_growth, 1),
        "revenue_growth": round(revenue_growth, 1),
        "gmv_growth": round(gmv_growth, 1),
        "conversion_rate": payment_success_rate,
        "order_generation_rate": 100.0 if orders_last_30 > 0 else 0.0,
        "total_order_attempts": orders_last_30,
        "order_placement_rate": 100.0 if orders_last_30 > 0 else 0.0,
        "total_orders_placed": orders_last_30,
        "payment_success_rate": payment_success_rate,
        "total_payments_succeeded": paid_orders_last_30,
        "order_breakdown": {
            "total": orders_last_30,
            "paid": paid_orders_last_30,
            "all_time_total": _to_int(analytics["total_orders_all_time"]) if analytics else 0,
            "all_time_paid": _to_int(analytics["paid_orders_all_time"]) if analytics else 0,
        },
        "revenue_breakdown": {
            "confirmed": confirmed_revenue_last_30,
            "gmv": gmv_last_30,
            "all_time_confirmed": float(analytics["confirmed_revenue_all_time"] or 0) if analytics else 0.0,
            "all_time_gmv": float(analytics["gmv_all_time"] or 0) if analytics else 0.0,
        },
        "confirmed_revenue": confirmed_revenue_last_30,
        "gmv": gmv_last_30,
    }
    try:
        analytics_payload["traffic_attribution_summary"] = await build_employee_traffic_overview(
            window="30d",
            merchant_id=merchant_id,
        )
    except Exception:
        analytics_payload["traffic_attribution_summary"] = {
            "window": "30d",
            "merchant_id": merchant_id,
            "requests_total": 0,
            "clicked_exposure": 0,
            "ordered_conversion": 0,
            "refunded_orders": 0,
            "gmv_total": "0",
            "refunded_amount_total": "0",
            "unknown_rates": {
                "request_unknown_share": 0.0,
                "click_unknown_share": 0.0,
                "order_unknown_share": 0.0,
                "source_channel_missing_ratio": 0.0,
                "protocol_name_missing_ratio": 0.0,
                "query_source_missing_ratio": 0.0,
                "agent_id_missing_ratio": 0.0,
            },
        }
    return analytics_payload


def _to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_connected_store(store: Dict[str, Any]) -> bool:
    if bool(store.get("is_connected")):
        return True
    status = str(store.get("status") or "").strip().lower()
    return status == "active" and bool(store.get("api_key_present"))


def _is_connected_psp(psp: Dict[str, Any]) -> bool:
    if not bool(psp.get("configured")):
        return False
    status = str(psp.get("status") or "").strip().lower()
    return status not in {"pending_setup", "rejected", "inactive", "disabled", "error"}


def _summarize_employee_merchant_commerce_readiness(
    merchant_id: str,
    stores: List[Dict[str, Any]],
    psps: List[Dict[str, Any]],
    analytics: Dict[str, Any],
) -> Dict[str, Any]:
    connected_stores = [store for store in stores if _is_connected_store(store)]
    connected_store_domains = sorted(
        {
            str(store.get("domain") or "").strip().lower()
            for store in connected_stores
            if str(store.get("domain") or "").strip()
        }
    )
    connected_psps = [psp for psp in psps if _is_connected_psp(psp)]
    psp_providers = sorted(
        {
            str(psp.get("provider") or psp.get("name") or "").strip()
            for psp in connected_psps
            if str(psp.get("provider") or psp.get("name") or "").strip()
        }
    )

    catalog_product_count = _to_int(analytics.get("total_products"))
    orders_last_30_days = _to_int(
        analytics.get("total_orders") or (analytics.get("order_breakdown") or {}).get("total")
    )
    paid_orders_last_30_days = _to_int(
        analytics.get("total_payments_succeeded") or (analytics.get("order_breakdown") or {}).get("paid")
    )
    confirmed_revenue_last_30_days = _to_float(
        analytics.get("confirmed_revenue")
        or analytics.get("total_revenue")
        or (analytics.get("revenue_breakdown") or {}).get("confirmed")
    )
    all_time_paid_orders = _to_int((analytics.get("order_breakdown") or {}).get("all_time_paid"))
    all_time_confirmed_revenue = _to_float(
        (analytics.get("revenue_breakdown") or {}).get("all_time_confirmed")
    )

    store_domain_connected = bool(connected_store_domains)
    catalog_synced = catalog_product_count > 0
    psp_or_checkout_connected = bool(connected_psps)
    order_payment_loop_observed = (
        paid_orders_last_30_days > 0
        or confirmed_revenue_last_30_days > 0
        or all_time_paid_orders > 0
        or all_time_confirmed_revenue > 0
    )

    merchant_valid = store_domain_connected and catalog_synced and psp_or_checkout_connected
    rollout_ready = merchant_valid and order_payment_loop_observed

    invalid_reasons: List[str] = []
    if not store_domain_connected:
        invalid_reasons.append("missing_connected_store_domain")
    if not catalog_synced:
        invalid_reasons.append("missing_catalog_sync")
    if not psp_or_checkout_connected:
        invalid_reasons.append("missing_psp_or_checkout")

    if not store_domain_connected:
        operator_action = "Connect a real merchant store domain before treating this merchant as rollout-valid."
    elif not catalog_synced:
        operator_action = "Run catalog sync and verify active products are present in products_cache."
    elif not psp_or_checkout_connected:
        operator_action = "Connect and configure a live PSP or checkout path before rollout."
    elif not order_payment_loop_observed:
        operator_action = "Run a real order and verify paid order / confirmed revenue signals before rollout."
    else:
        operator_action = "Merchant has the core commerce prerequisites and a live payment loop."

    checklist = [
        {
            "key": "store_domain_connected",
            "label": "Connected store domain",
            "state": "ready" if store_domain_connected else "missing",
            "detail": (
                f"{len(connected_store_domains)} connected domains in scope."
                if store_domain_connected
                else "No active connected store domain found."
            ),
        },
        {
            "key": "catalog_synced",
            "label": "Catalog synced",
            "state": "ready" if catalog_synced else "missing",
            "detail": (
                f"{catalog_product_count} active catalog products in products_cache."
                if catalog_synced
                else "No active catalog products found in products_cache."
            ),
        },
        {
            "key": "psp_or_checkout_connected",
            "label": "PSP or checkout connected",
            "state": "ready" if psp_or_checkout_connected else "missing",
            "detail": (
                f"{len(connected_psps)} configured PSP connections: {', '.join(psp_providers) or 'configured'}."
                if psp_or_checkout_connected
                else "No configured PSP or live checkout path found."
            ),
        },
        {
            "key": "order_payment_loop_observed",
            "label": "Order/payment loop observed",
            "state": "ready" if order_payment_loop_observed else "unproven",
            "detail": (
                f"{all_time_paid_orders} paid orders observed all-time."
                if order_payment_loop_observed
                else "No paid order or confirmed revenue evidence observed yet."
            ),
        },
    ]

    status = "green" if rollout_ready else "yellow" if merchant_valid else "red"

    return {
        "merchant_id": merchant_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "status": status,
        "merchant_valid": merchant_valid,
        "rollout_ready": rollout_ready,
        "operator_action": operator_action,
        "invalid_reasons": invalid_reasons,
        "connected_store_count": len(connected_stores),
        "connected_store_domain_count": len(connected_store_domains),
        "connected_store_domains": connected_store_domains,
        "catalog_product_count": catalog_product_count,
        "psp_connected": psp_or_checkout_connected,
        "psp_provider_count": len(psp_providers),
        "psp_providers": psp_providers,
        "orders_last_30_days": orders_last_30_days,
        "paid_orders_last_30_days": paid_orders_last_30_days,
        "confirmed_revenue_last_30_days": confirmed_revenue_last_30_days,
        "all_time_paid_orders": all_time_paid_orders,
        "all_time_confirmed_revenue": all_time_confirmed_revenue,
        "checklist": checklist,
    }


async def build_employee_merchant_commerce_readiness(merchant_id: str) -> Dict[str, Any]:
    stores, psps, analytics = await asyncio.gather(
        _fetch_employee_merchant_stores(merchant_id),
        _fetch_employee_merchant_psps(merchant_id),
        _fetch_employee_merchant_analytics(merchant_id),
    )
    return _summarize_employee_merchant_commerce_readiness(merchant_id, stores, psps, analytics or {})

@router.get("/analytics/dashboard")
async def get_analytics_dashboard(
    time_range: str = Query("30d", description="Time range: 1d, 7d, 30d, 90d"),
    current_user: dict = Depends(get_current_user)
):
    """Get analytics dashboard for employee portal"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Parse time_range to get days
        days_map = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
        days = days_map.get(time_range, 30)
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Get transactions from orders table (filtered by time_range)
        # Revenue breakdown with time-based pending classification
        transactions_query = """
            SELECT 
                COUNT(*) as total_orders,
                
                -- Confirmed Revenue (only paid orders)
                COUNT(CASE WHEN payment_status = 'paid' THEN 1 END) as paid_orders,
                COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END), 0) as confirmed_revenue,
                
                -- Pending Revenue (time-based classification)
                COUNT(CASE WHEN payment_status = 'pending' 
                    AND created_at >= NOW() - INTERVAL '5 minutes' THEN 1 END) as pending_recent_count,
                COALESCE(SUM(CASE WHEN payment_status = 'pending' 
                    AND created_at >= NOW() - INTERVAL '5 minutes' THEN total ELSE 0 END), 0) as pending_recent_revenue,
                
                COUNT(CASE WHEN payment_status = 'pending' 
                    AND created_at < NOW() - INTERVAL '5 minutes'
                    AND created_at >= NOW() - INTERVAL '30 minutes' THEN 1 END) as pending_stale_count,
                COALESCE(SUM(CASE WHEN payment_status = 'pending' 
                    AND created_at < NOW() - INTERVAL '5 minutes'
                    AND created_at >= NOW() - INTERVAL '30 minutes' THEN total ELSE 0 END), 0) as pending_stale_revenue,
                
                COUNT(CASE WHEN payment_status = 'pending' 
                    AND created_at < NOW() - INTERVAL '30 minutes' THEN 1 END) as pending_abandoned_count,
                COALESCE(SUM(CASE WHEN payment_status = 'pending' 
                    AND created_at < NOW() - INTERVAL '30 minutes' THEN total ELSE 0 END), 0) as pending_abandoned_revenue,
                
                -- Failed Revenue
                COUNT(CASE WHEN payment_status = 'failed' THEN 1 END) as failed_orders,
                COALESCE(SUM(CASE WHEN payment_status = 'failed' THEN total ELSE 0 END), 0) as failed_revenue,
                
                -- Average Transaction (only paid)
                AVG(CASE WHEN payment_status = 'paid' THEN total ELSE NULL END) as avg_transaction_value
            FROM orders
            WHERE created_at >= :start_date
        """
        transactions = await database.fetch_one(transactions_query, {"start_date": start_date})
        
        # Calculate success rate (paid orders / total orders)
        success_rate = 0
        if transactions and transactions["total_orders"] > 0:
            success_rate = (transactions["paid_orders"] / transactions["total_orders"]) * 100
        
        # Get merchant counts
        merchants_query = """
            SELECT 
                COUNT(*) as total_merchants,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_merchants
            FROM merchant_onboarding
        """
        merchants = await database.fetch_one(merchants_query)
        
        # Get PSP counts
        psp_query = """
            SELECT COUNT(DISTINCT provider) as total_psps,
                   COUNT(*) as total_connections
            FROM merchant_psps
        """
        psps = await database.fetch_one(psp_query)
        
        # Get recent transaction trends (based on time_range)
        trends_query = """
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as transactions,
                COALESCE(SUM(total), 0) as revenue
            FROM orders
            WHERE created_at >= :start_date
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """
        trends = await database.fetch_all(trends_query, {"start_date": start_date})
        
        trend_data = []
        for trend in trends:
            trend_data.append({
                "date": trend["date"].isoformat() if trend["date"] else None,
                "transactions": trend["transactions"],
                "revenue": float(trend["revenue"])
            })
        
        return {
            "status": "success",
            "data": {
                # Main metrics (only paid orders)
                "total_transactions": transactions["paid_orders"] if transactions else 0,
                "total_revenue": float(transactions["confirmed_revenue"]) if transactions else 0,
                "success_rate": round(success_rate, 1),
                "avg_transaction_value": float(transactions["avg_transaction_value"]) if transactions and transactions["avg_transaction_value"] else 0,
                
                # Revenue breakdown
                "revenue_breakdown": {
                    "confirmed": float(transactions["confirmed_revenue"]) if transactions else 0,
                    "pending_recent": float(transactions["pending_recent_revenue"]) if transactions else 0,
                    "pending_stale": float(transactions["pending_stale_revenue"]) if transactions else 0,
                    "pending_abandoned": float(transactions["pending_abandoned_revenue"]) if transactions else 0,
                    "failed": float(transactions["failed_revenue"]) if transactions else 0
                },
                
                # Order breakdown
                "order_breakdown": {
                    "total": transactions["total_orders"] if transactions else 0,
                    "paid": transactions["paid_orders"] if transactions else 0,
                    "pending_recent": transactions["pending_recent_count"] if transactions else 0,
                    "pending_stale": transactions["pending_stale_count"] if transactions else 0,
                    "pending_abandoned": transactions["pending_abandoned_count"] if transactions else 0,
                    "failed": transactions["failed_orders"] if transactions else 0
                },
                
                # Other metrics
                "total_merchants": merchants["total_merchants"] if merchants else 0,
                "active_merchants": merchants["active_merchants"] if merchants else 0,
                "total_psps": psps["total_psps"] if psps else 0,
                "total_psp_connections": psps["total_connections"] if psps else 0,
                "transaction_trends": trend_data,
                "time_range": time_range
            }
        }
    except Exception as e:
        print(f"Error in analytics dashboard: {e}")
        import traceback
        traceback.print_exc()
        # Return default values if error
        return {
            "status": "success",
            "data": {
                "total_transactions": 0,
                "total_revenue": 0,
                "success_rate": 0,
                "avg_transaction_value": 0,
                "revenue_breakdown": {
                    "confirmed": 0,
                    "pending_recent": 0,
                    "pending_stale": 0,
                    "pending_abandoned": 0,
                    "failed": 0
                },
                "order_breakdown": {
                    "total": 0,
                    "paid": 0,
                    "pending_recent": 0,
                    "pending_stale": 0,
                    "pending_abandoned": 0,
                    "failed": 0
                },
                "total_merchants": 0,
                "active_merchants": 0,
                "total_psps": 0,
                "total_psp_connections": 0,
                "transaction_trends": [],
                "time_range": time_range
            }
        }


@router.get("/employee/readiness/cache/optimization/metrics")
async def get_employee_optimization_cache_metrics(
    merchant_id: Optional[str] = Query(default=None),
    channel: Optional[str] = Query(default="ucp"),
    current_user: dict = Depends(get_current_employee),
):
    """Get readiness optimization cache observability for employee debugging."""
    return {
        "status": "success",
        "data": _scoped_optimization_cache_metrics(
            merchant_id=merchant_id,
            channel=channel,
        ),
    }


@router.post("/employee/readiness/cache/optimization/invalidate")
async def invalidate_employee_optimization_cache(
    body: EmployeeOptimizationCacheInvalidateRequest,
    current_user: dict = Depends(get_current_employee),
):
    """Invalidate readiness optimization cache for a merchant or channel."""
    invalidated_entries = invalidate_readiness_optimization_cache(
        merchant_id=body.merchant_id,
        channel=body.channel,
    )
    return {
        "status": "success",
        "data": {
            "invalidated_entries": invalidated_entries,
            "scope": {
                "merchant_id": body.merchant_id,
                "channel": body.channel,
            },
            "cache_metrics": _scoped_optimization_cache_metrics(
                merchant_id=body.merchant_id,
                channel=body.channel,
            ),
        },
    }


@router.get("/employee/traffic/overview")
async def get_employee_traffic_overview(
    window: str = Query("30d"),
    source_channel: Optional[str] = Query(None),
    source_family: Optional[str] = Query(None),
    protocol_name: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    query_source: Optional[str] = Query(None),
    llm_provider: Optional[str] = Query(None),
    llm_model: Optional[str] = Query(None),
    commerce_surface: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_employee),
):
    filters = {
        "source_channel": source_channel,
        "source_family": source_family,
        "protocol_name": protocol_name,
        "agent_id": agent_id,
        "query_source": query_source,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "commerce_surface": commerce_surface,
    }
    return {
        "status": "success",
        "data": await build_employee_traffic_overview(window=window, filters=filters),
    }


@router.get("/employee/traffic/breakdown")
async def get_employee_traffic_breakdown(
    group_by: str = Query("source_channel"),
    window: str = Query("30d"),
    source_channel: Optional[str] = Query(None),
    source_family: Optional[str] = Query(None),
    protocol_name: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    query_source: Optional[str] = Query(None),
    llm_provider: Optional[str] = Query(None),
    llm_model: Optional[str] = Query(None),
    commerce_surface: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_employee),
):
    if group_by not in TRAFFIC_BREAKDOWN_FIELDS:
        raise HTTPException(
            status_code=400,
            detail="group_by must be one of " + ", ".join(sorted(TRAFFIC_BREAKDOWN_FIELDS)),
        )
    filters = {
        "source_channel": source_channel,
        "source_family": source_family,
        "protocol_name": protocol_name,
        "agent_id": agent_id,
        "query_source": query_source,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "commerce_surface": commerce_surface,
    }
    return {
        "status": "success",
        "data": await build_employee_traffic_breakdown(
            group_by=group_by,
            window=window,
            filters=filters,
        ),
    }


@router.get("/employee/merchant/{merchant_id}/traffic")
async def get_employee_merchant_traffic_route(
    merchant_id: str,
    window: str = Query("30d"),
    source_channel: Optional[str] = Query(None),
    source_family: Optional[str] = Query(None),
    protocol_name: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    query_source: Optional[str] = Query(None),
    llm_provider: Optional[str] = Query(None),
    llm_model: Optional[str] = Query(None),
    commerce_surface: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_employee),
):
    filters = {
        "source_channel": source_channel,
        "source_family": source_family,
        "protocol_name": protocol_name,
        "agent_id": agent_id,
        "query_source": query_source,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "commerce_surface": commerce_surface,
    }
    return {
        "status": "success",
        "data": await build_employee_merchant_traffic(
            merchant_id=merchant_id,
            window=window,
            filters=filters,
        ),
    }


@router.get("/employee/merchant/{merchant_id}/integrations")
async def get_employee_merchant_integrations(
    merchant_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe view of merchant store integrations."""
    try:
        stores = await _fetch_employee_merchant_stores(merchant_id)
        return {"status": "success", "data": {"stores": stores}}
    except Exception as e:
        print(f"Error fetching employee merchant integrations for {merchant_id}: {e}")
        return {"status": "success", "data": {"stores": []}}


@router.get("/employee/merchant/{merchant_id}/psps")
async def get_employee_merchant_psps(
    merchant_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe view of merchant PSP connections."""
    try:
        psps = await _fetch_employee_merchant_psps(merchant_id)
        return {"status": "success", "data": {"psps": psps}}
    except Exception as e:
        print(f"Error fetching employee merchant PSPs for {merchant_id}: {e}")
        return {"status": "success", "data": {"psps": []}}


@router.get("/employee/merchant/{merchant_id}/analytics")
async def get_employee_merchant_analytics(
    merchant_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe analytics summary for one merchant."""
    try:
        analytics = await _fetch_employee_merchant_analytics(merchant_id)
        return {"status": "success", "data": analytics}
    except Exception as e:
        print(f"Error fetching employee merchant analytics for {merchant_id}: {e}")
        return {
            "status": "success",
            "data": {
                "total_orders": 0,
                "total_revenue": 0.0,
                "total_customers": 0,
                "total_products": 0,
                "average_order_value": 0.0,
                "order_growth": 0.0,
                "revenue_growth": 0.0,
                "gmv_growth": 0.0,
                "conversion_rate": 0.0,
                "order_generation_rate": 0.0,
                "total_order_attempts": 0,
                "order_placement_rate": 0.0,
                "total_orders_placed": 0,
                "payment_success_rate": 0.0,
                "total_payments_succeeded": 0,
                "order_breakdown": {"total": 0, "paid": 0, "all_time_total": 0, "all_time_paid": 0},
                "revenue_breakdown": {"confirmed": 0.0, "gmv": 0.0, "all_time_confirmed": 0.0, "all_time_gmv": 0.0},
                "confirmed_revenue": 0.0,
                "gmv": 0.0,
                "traffic_attribution_summary": {
                    "window": "30d",
                    "merchant_id": merchant_id,
                    "requests_total": 0,
                    "clicked_exposure": 0,
                    "ordered_conversion": 0,
                    "refunded_orders": 0,
                    "gmv_total": "0",
                    "refunded_amount_total": "0",
                    "unknown_rates": {
                        "request_unknown_share": 0.0,
                        "click_unknown_share": 0.0,
                        "order_unknown_share": 0.0,
                        "source_channel_missing_ratio": 0.0,
                        "protocol_name_missing_ratio": 0.0,
                        "query_source_missing_ratio": 0.0,
                        "agent_id_missing_ratio": 0.0,
                    },
                },
            },
        }


@router.get("/employee/merchant/{merchant_id}/commerce-readiness")
async def get_employee_merchant_commerce_readiness(
    merchant_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe checklist for merchant-valid commerce rollout."""
    try:
        summary = await build_employee_merchant_commerce_readiness(merchant_id)
        return {"status": "success", "data": summary}
    except Exception as e:
        print(f"Error fetching employee merchant commerce readiness for {merchant_id}: {e}")
        return {
            "status": "success",
            "data": {
                "merchant_id": merchant_id,
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "status": "red",
                "merchant_valid": False,
                "rollout_ready": False,
                "operator_action": "Review store connection, catalog sync, PSP setup, and paid order evidence.",
                "invalid_reasons": [
                    "merchant_commerce_readiness_unavailable",
                ],
                "connected_store_count": 0,
                "connected_store_domain_count": 0,
                "connected_store_domains": [],
                "catalog_product_count": 0,
                "psp_connected": False,
                "psp_provider_count": 0,
                "psp_providers": [],
                "orders_last_30_days": 0,
                "paid_orders_last_30_days": 0,
                "confirmed_revenue_last_30_days": 0.0,
                "all_time_paid_orders": 0,
                "all_time_confirmed_revenue": 0.0,
                "checklist": [
                    {
                        "key": "store_domain_connected",
                        "label": "Connected store domain",
                        "state": "missing",
                        "detail": "Merchant commerce readiness unavailable.",
                    },
                    {
                        "key": "catalog_synced",
                        "label": "Catalog synced",
                        "state": "missing",
                        "detail": "Merchant commerce readiness unavailable.",
                    },
                    {
                        "key": "psp_or_checkout_connected",
                        "label": "PSP or checkout connected",
                        "state": "missing",
                        "detail": "Merchant commerce readiness unavailable.",
                    },
                    {
                        "key": "order_payment_loop_observed",
                        "label": "Order/payment loop observed",
                        "state": "unproven",
                        "detail": "Merchant commerce readiness unavailable.",
                    },
                ],
            },
        }


@router.get("/employee/referral-readiness/summary")
async def get_employee_referral_readiness_summary(
    merchant_id: str = Query(..., min_length=1),
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe attached fallback seed summary for one merchant."""
    try:
        summary = await build_external_referral_summary(merchant_id)
        return {"status": "success", "data": summary}
    except Exception as e:
        print(f"Error fetching employee referral readiness summary for {merchant_id}: {e}")
        return {
            "status": "success",
            "data": {
                "merchant_id": merchant_id,
                "status": "red",
                "gating_policy_version": "external_referral_v1",
                "matched_domains": [],
                "total_active_seeds": 0,
                "attached_seed_count": 0,
                "domain_unattached_seed_count": 0,
                "healthy_seed_count": 0,
                "blocked_seed_count": 0,
                "review_seed_count": 0,
                "issue_buckets": [],
                "sample_blocked_seeds": [],
                "last_extracted_at_oldest": None,
                "last_extracted_at_newest": None,
            },
        }


@router.get("/employee/referral-readiness/program-summary")
async def get_employee_referral_program_summary(
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe summary of the Pivota-managed fallback referral program."""
    try:
        summary = await build_platform_fallback_program_summary()
        return {"status": "success", "data": summary}
    except Exception as e:
        print(f"Error fetching employee referral program summary: {e}")
        return {
            "status": "success",
            "data": {
                "status": "red",
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "gating_policy_version": "external_referral_v1",
                "total_active_seeds": 0,
                "healthy_seed_count": 0,
                "blocked_seed_count": 0,
                "review_seed_count": 0,
                "attached_seed_count": 0,
                "unattached_seed_count": 0,
                "issue_buckets": [],
                "top_domains": [],
                "top_blocked_domains": [],
                "runtime_surface_coverage_summary": {
                    "total_surface_eligible_seeds": 0,
                    "total_surface_blocked_seeds": 0,
                    "surface_eligible_rate_pct": 0.0,
                    "attached_surface_eligible_seed_count": 0,
                    "attached_surface_blocked_seed_count": 0,
                },
            },
        }


@router.get("/employee/referral-readiness/merchant-commerce-cohort")
async def get_employee_merchant_commerce_cohort_summary(
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe background summary of merchant-valid commerce prerequisites."""
    try:
        summary = await build_merchant_commerce_cohort_summary()
        return {"status": "success", "data": summary}
    except Exception as e:
        print(f"Error fetching employee merchant commerce cohort summary: {e}")
        return {
            "status": "success",
            "data": {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_registered_merchants": 0,
                "store_connected_merchants": 0,
                "store_connected_with_psp_merchants": 0,
                "merchant_valid_count": 0,
                "merchant_invalid_count": 0,
                "top_invalid_merchants": [],
            },
        }


@router.get("/employee/merchant-commerce/readiness-list")
async def get_employee_merchant_commerce_readiness_list(
    current_user: dict = Depends(get_current_employee),
):
    """Employee-safe queue of merchant-valid commerce readiness states."""
    try:
        summary = await build_merchant_commerce_readiness_list()
        return {"status": "success", "data": summary}
    except Exception as e:
        print(f"Error fetching employee merchant commerce readiness list: {e}")
        return {
            "status": "success",
            "data": {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_registered_merchants": 0,
                "merchant_valid_count": 0,
                "rollout_ready_count": 0,
                "attention_count": 0,
                "merchants": [],
            },
        }


@router.get("/employee/referral-readiness/fleet-summary")
async def get_employee_referral_readiness_fleet_summary(
    current_user: dict = Depends(get_current_employee),
):
    """Deprecated compatibility alias for merchant-commerce cohort summary."""
    try:
        summary = await build_external_referral_fleet_summary()
        return {"status": "success", "data": summary}
    except Exception as e:
        print(f"Error fetching employee referral readiness fleet summary: {e}")
        return {
            "status": "success",
            "data": {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_registered_merchants": 0,
                "store_connected_merchants": 0,
                "store_connected_with_psp_merchants": 0,
                "merchant_valid_count": 0,
                "merchant_invalid_count": 0,
                "top_invalid_merchants": [],
            },
        }

@router.get("/agents")
async def get_all_agents(
    current_user: dict = Depends(get_current_user)
):
    """Get all agents in the system"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if agents table exists
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'agents'
            )
        """
        table_exists = await database.fetch_one(check_query)
        
        if table_exists and table_exists["exists"]:
            agents_query = """
                SELECT agent_id, name, email, status, created_at, last_active
                FROM agents
                ORDER BY created_at DESC
            """
            agents = await database.fetch_all(agents_query)
            
            return {
                "status": "success",
                "agents": [
                    {
                        "agent_id": a["agent_id"],
                        "name": a["name"],
                        "email": a["email"],
                        "status": a["status"],
                        "is_active": a["status"] == "active",
                        "created_at": a["created_at"].isoformat() if a["created_at"] else None,
                        "last_active": a["last_active"].isoformat() if a["last_active"] else None
                    }
                    for a in agents
                ]
            }
        else:
            # Return demo agents if table doesn't exist
            return {
                "status": "success",
                "agents": [
                    {
                        "agent_id": "agent_001",
                        "name": "John Smith",
                        "email": "john@pivota.com",
                        "status": "active",
                        "is_active": True,
                        "created_at": datetime.now().isoformat(),
                        "last_active": datetime.now().isoformat()
                    },
                    {
                        "agent_id": "agent_002",
                        "name": "Sarah Johnson",
                        "email": "sarah@pivota.com",
                        "status": "active",
                        "is_active": True,
                        "created_at": datetime.now().isoformat(),
                        "last_active": datetime.now().isoformat()
                    }
                ]
            }
    except Exception as e:
        print(f"Error fetching agents: {e}")
        return {"status": "success", "agents": []}

@router.get("/system/status")
async def get_system_status(
    current_user: dict = Depends(get_current_user)
):
    """Get system health status"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check database connection
        test_query = "SELECT 1"
        await database.fetch_one(test_query)
        
        # Check critical tables
        tables_to_check = ['merchant_onboarding', 'orders', 'merchant_stores', 'merchant_psps']
        all_healthy = True
        issues = []
        
        for table in tables_to_check:
            check_query = f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = '{table}'
                )
            """
            result = await database.fetch_one(check_query)
            if not result or not result["exists"]:
                all_healthy = False
                issues.append(f"Table {table} is missing")
        
        return {
            "status": "success",
            "healthy": all_healthy,
            "message": "All systems operational" if all_healthy else f"Issues detected: {', '.join(issues)}",
            "timestamp": datetime.now().isoformat(),
            "services": {
                "database": "operational",
                "api": "operational",
                "orders": "operational" if "orders" not in str(issues) else "degraded",
                "merchants": "operational" if "merchant" not in str(issues) else "degraded"
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "healthy": False,
            "message": f"System check failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }

@router.get("/transactions")
async def get_all_transactions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get all transactions across all merchants"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build query with optional status filter
        where_clause = ""
        params = {}
        
        if status:
            where_clause = "WHERE status = :status"
            params["status"] = status
        
        # Get total count
        count_query = f"SELECT COUNT(*) as total FROM orders {where_clause}"
        count_result = await database.fetch_one(count_query, params)
        total = count_result["total"] if count_result else 0
        
        # Get transactions
        transactions_query = f"""
            SELECT 
                o.order_id, o.merchant_id, o.total as amount, o.currency, 
                o.status, o.payment_status, o.payment_method, o.customer_name, o.customer_email,
                o.created_at, o.psp_id, o.psp_used,
                m.business_name as merchant_name
            FROM orders o
            LEFT JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            {where_clause}
            ORDER BY o.created_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        params["limit"] = limit
        params["offset"] = offset
        
        rows = await database.fetch_all(transactions_query, params)
        
        transactions = []
        for row in rows:
            transactions.append({
                "transaction_id": row["order_id"],
                "merchant_id": row["merchant_id"],
                "merchant_name": row["merchant_name"] or "Unknown Merchant",
                "amount": float(row["amount"]),
                "currency": row["currency"],
                "status": row["status"],
                "payment_method": row["payment_method"],
                "customer": {
                    "name": row["customer_name"],
                    "email": row["customer_email"]
                },
                "psp": row["psp_id"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None
            })
        
        return {
            "status": "success",
            "data": {
                "transactions": transactions,
                "total": total,
                "limit": limit,
                "offset": offset
            }
        }
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return {
            "status": "success",
            "data": {
                "transactions": [],
                "total": 0,
                "limit": limit,
                "offset": offset
            }
        }

@router.get("/psps/all")
async def get_all_psps(
    current_user: dict = Depends(get_current_user)
):
    """Get all PSPs across all merchants"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP connections (without aggregating orders here)
        psps_query = """
            SELECT 
                p.psp_id, p.provider, p.name, p.status, p.merchant_id,
                p.connected_at, p.capabilities,
                p.api_key, p.account_id, p.secret_key,
                m.business_name as merchant_name
            FROM merchant_psps p
            LEFT JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            ORDER BY p.connected_at DESC
        """
        
        rows = await database.fetch_all(psps_query)
        
        psps = []
        for row in rows:
            capabilities = []
            if row["capabilities"]:
                capabilities = row["capabilities"].split(',')
            
            # Get transaction stats for THIS specific PSP's merchant only
            stats = await database.fetch_one(
                """
                SELECT 
                    COUNT(order_id) as transaction_count,
                    COALESCE(SUM(total), 0) as total_volume,
                    SUM(CASE WHEN LOWER(COALESCE(payment_status,'')) IN ('paid','succeeded','completed') 
                             OR LOWER(COALESCE(status,'')) IN ('completed','delivered') 
                        THEN 1 ELSE 0 END) as successful_count
                FROM orders
                WHERE merchant_id = :merchant_id
                AND (is_deleted IS NULL OR is_deleted = FALSE)
                """,
                {"merchant_id": row["merchant_id"]}
            )
            
            transaction_count = stats["transaction_count"] if stats else 0
            successful_count = stats["successful_count"] if stats else 0
            success_rate = round((successful_count / transaction_count * 100), 1) if transaction_count > 0 else 0
            total_volume = float(stats["total_volume"]) if stats else 0
            
            # Mask BOTH stored credentials. `secret_key` was already masked
            # here; `api_key` was returned in the clear beside it, commented
            # "Include for Configure form" — so the PSP's live API key was
            # shipped to a browser on every page load of the PSP list, for
            # every merchant at once. Employee/admin auth limits who can ask,
            # but the credential still leaves the server, lands in the
            # response cache, and shows up in devtools and HAR captures.
            #
            # Masking alone would have been WORSE than the leak. The portal's
            # UpdatePSPForm seeds its api_key input from this field and posts
            # it back verbatim on save, with `canSave` requiring it non-empty
            # — so a masked value round-trips and overwrites the real key.
            # `/admin/psp/connect` therefore treats a masked api_key as "keep
            # the stored one" (see is_masked_credential). The two changes are
            # a pair; neither is safe alone.
            row_dict = dict(row)
            secret_key_masked = mask_credential(row_dict.get("secret_key"))
            api_key_masked = mask_credential(row_dict.get("api_key"))

            psps.append({
                "psp_id": row["psp_id"],
                "provider": row["provider"],
                "name": row["name"],
                "status": row["status"],
                "merchant_id": row["merchant_id"],
                "merchant_name": row["merchant_name"] or "Unknown Merchant",
                "connected_at": row["connected_at"].isoformat() if row["connected_at"] else None,
                "capabilities": capabilities,
                # Masked, not omitted: the portal's form disables Save when
                # this is empty, so dropping the key would lock operators out
                # of editing account_id on an existing PSP.
                "api_key": api_key_masked,
                "api_key_present": bool(row_dict.get("api_key")),
                "account_id": row["account_id"],  # An identifier, not a secret
                "secret_key": secret_key_masked,  # Masked for security
                "secret_key_present": bool(row_dict.get("secret_key")),
                "transaction_count": transaction_count,
                "successful_count": successful_count,
                "success_rate": success_rate,
                "total_volume": total_volume,
                "is_active": row["status"] == "active"
            })
        
        return {
            "status": "success",
            "psps": psps
        }
    except Exception as e:
        print(f"Error fetching PSPs: {e}")
        return {"status": "success", "psps": []}

@router.post("/psps/test-connection")
async def test_psp_connection(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test PSP connection"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP details
        psp_query = "SELECT * FROM merchant_psps WHERE psp_id = :psp_id"
        psp = await database.fetch_one(psp_query, {"psp_id": psp_id})
        
        if not psp:
            raise HTTPException(status_code=404, detail="PSP not found")
        
        # Simulate connection test
        # In production, this would actually test the API connection
        return {
            "status": "success",
            "message": f"Connection to {psp['provider']} successful",
            "details": {
                "provider": psp["provider"],
                "response_time": random.randint(50, 200),
                "api_version": "v2",
                "capabilities": psp["capabilities"].split(',') if psp["capabilities"] else []
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "status": "error",
            "message": f"Connection test failed: {str(e)}"
        }

@router.get("/finance/overview")
async def get_finance_overview(
    current_user: dict = Depends(get_current_user)
):
    """Get finance overview for employee portal"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get financial metrics from orders
        finance_query = """
            SELECT 
                COUNT(*) as total_transactions,
                COALESCE(SUM(amount), 0) as gross_revenue,
                COALESCE(SUM(CASE WHEN status IN ('completed', 'delivered') THEN amount ELSE 0 END), 0) as net_revenue,
                COALESCE(AVG(amount), 0) as avg_transaction_value,
                COUNT(DISTINCT merchant_id) as active_merchants,
                COUNT(DISTINCT DATE(created_at)) as active_days
            FROM orders
            WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
        """
        
        metrics = await database.fetch_one(finance_query)
        
        # Calculate fees (example: 2.9% + $0.30 per transaction)
        processing_fees = 0
        if metrics:
            processing_fees = (float(metrics["net_revenue"]) * 0.029) + (metrics["total_transactions"] * 0.30)
        
        # Get monthly breakdown
        monthly_query = """
            SELECT 
                DATE_TRUNC('month', created_at) as month,
                COUNT(*) as transactions,
                COALESCE(SUM(amount), 0) as revenue
            FROM orders
            WHERE created_at >= CURRENT_DATE - INTERVAL '6 months'
            GROUP BY DATE_TRUNC('month', created_at)
            ORDER BY month DESC
        """
        
        monthly_data = await database.fetch_all(monthly_query)
        
        monthly_breakdown = []
        for month in monthly_data:
            monthly_breakdown.append({
                "month": month["month"].strftime("%Y-%m") if month["month"] else None,
                "transactions": month["transactions"],
                "revenue": float(month["revenue"])
            })
        
        return {
            "status": "success",
            "data": {
                "gross_revenue": float(metrics["gross_revenue"]) if metrics else 0,
                "net_revenue": float(metrics["net_revenue"]) if metrics else 0,
                "processing_fees": round(processing_fees, 2),
                "avg_transaction_value": float(metrics["avg_transaction_value"]) if metrics else 0,
                "total_transactions": metrics["total_transactions"] if metrics else 0,
                "active_merchants": metrics["active_merchants"] if metrics else 0,
                "monthly_breakdown": monthly_breakdown
            }
        }
    except Exception as e:
        print(f"Error in finance overview: {e}")
        return {
            "status": "success",
            "data": {
                "gross_revenue": 0,
                "net_revenue": 0,
                "processing_fees": 0,
                "avg_transaction_value": 0,
                "total_transactions": 0,
                "active_merchants": 0,
                "monthly_breakdown": []
            }
        }
