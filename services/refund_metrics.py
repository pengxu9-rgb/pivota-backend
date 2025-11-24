"""
Refund Metrics and Monitoring
Tracks refund performance, rates, and alerts
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from db.database import database
from utils.logger import logger


class RefundMetrics:
    """Service for tracking and querying refund metrics"""
    
    @staticmethod
    async def get_refund_stats(
        merchant_id: Optional[str] = None,
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Get refund statistics for a merchant or globally
        
        Args:
            merchant_id: Optional merchant ID (None for global stats)
            days: Number of days to look back
            
        Returns:
            Dict with refund metrics
        """
        since_date = datetime.now() - timedelta(days=days)
        
        base_query = """
        SELECT 
            COUNT(*) as total_refunds,
            COUNT(DISTINCT order_id) as orders_refunded,
            SUM(amount) as total_refunded_amount,
            AVG(amount) as avg_refund_amount,
            COUNT(CASE WHEN status = 'completed' THEN 1 END) as successful_refunds,
            COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_refunds,
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_refunds,
            COUNT(CASE WHEN source = 'pivota_merchant' THEN 1 END) as merchant_initiated,
            COUNT(CASE WHEN source = 'platform_webhook' THEN 1 END) as platform_initiated
        FROM refund_records
        WHERE created_at >= :since_date
        """
        
        params = {"since_date": since_date}
        
        if merchant_id:
            base_query += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        
        result = await database.fetch_one(base_query, params)
        
        # Calculate refund rate (refunds / total orders)
        orders_query = """
        SELECT COUNT(*) as total_orders
        FROM orders
        WHERE created_at >= :since_date
        """
        
        if merchant_id:
            orders_query += " AND merchant_id = :merchant_id"
        
        orders_result = await database.fetch_one(orders_query, params)
        
        total_orders = orders_result["total_orders"] if orders_result else 0
        orders_refunded = result["orders_refunded"] if result else 0
        
        refund_rate = (orders_refunded / total_orders * 100) if total_orders > 0 else 0
        
        return {
            "period_days": days,
            "total_refunds": result["total_refunds"] or 0,
            "orders_refunded": orders_refunded,
            "total_orders": total_orders,
            "refund_rate_percent": round(refund_rate, 2),
            "total_refunded_amount": float(result["total_refunded_amount"] or 0),
            "avg_refund_amount": float(result["avg_refund_amount"] or 0),
            "successful_refunds": result["successful_refunds"] or 0,
            "failed_refunds": result["failed_refunds"] or 0,
            "pending_refunds": result["pending_refunds"] or 0,
            "merchant_initiated": result["merchant_initiated"] or 0,
            "platform_initiated": result["platform_initiated"] or 0
        }
    
    @staticmethod
    async def get_refund_breakdown_by_reason(
        merchant_id: Optional[str] = None,
        days: int = 30
    ) -> list[Dict[str, Any]]:
        """Get refund breakdown by reason"""
        since_date = datetime.now() - timedelta(days=days)
        
        query = """
        SELECT 
            reason,
            COUNT(*) as count,
            SUM(amount) as total_amount
        FROM refund_records
        WHERE created_at >= :since_date
        """
        
        params = {"since_date": since_date}
        
        if merchant_id:
            query += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        
        query += """
        GROUP BY reason
        ORDER BY count DESC
        LIMIT 10
        """
        
        results = await database.fetch_all(query, params)
        
        return [
            {
                "reason": r["reason"],
                "count": r["count"],
                "total_amount": float(r["total_amount"])
            }
            for r in results
        ]
    
    @staticmethod
    async def get_refund_trend(
        merchant_id: Optional[str] = None,
        days: int = 30
    ) -> list[Dict[str, Any]]:
        """Get daily refund trend"""
        since_date = datetime.now() - timedelta(days=days)
        
        query = """
        SELECT 
            DATE(created_at) as date,
            COUNT(*) as refund_count,
            SUM(amount) as refund_amount
        FROM refund_records
        WHERE created_at >= :since_date
        """
        
        params = {"since_date": since_date}
        
        if merchant_id:
            query += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        
        query += """
        GROUP BY DATE(created_at)
        ORDER BY date DESC
        """
        
        results = await database.fetch_all(query, params)
        
        return [
            {
                "date": r["date"].isoformat() if r["date"] else None,
                "refund_count": r["refund_count"],
                "refund_amount": float(r["refund_amount"])
            }
            for r in results
        ]
    
    @staticmethod
    async def check_high_refund_rate_merchants(
        threshold_percent: float = 15.0,
        days: int = 30
    ) -> list[Dict[str, Any]]:
        """
        Identify merchants with high refund rates
        
        Args:
            threshold_percent: Refund rate threshold (default 15%)
            days: Period to analyze
            
        Returns:
            List of merchants exceeding threshold
        """
        since_date = datetime.now() - timedelta(days=days)
        
        query = """
        WITH refund_stats AS (
            SELECT 
                merchant_id,
                COUNT(DISTINCT order_id) as orders_refunded,
                SUM(amount) as total_refunded
            FROM refund_records
            WHERE created_at >= :since_date
            GROUP BY merchant_id
        ),
        order_stats AS (
            SELECT 
                merchant_id,
                COUNT(*) as total_orders
            FROM orders
            WHERE created_at >= :since_date
            GROUP BY merchant_id
        )
        SELECT 
            r.merchant_id,
            r.orders_refunded,
            o.total_orders,
            (r.orders_refunded::float / o.total_orders * 100) as refund_rate,
            r.total_refunded
        FROM refund_stats r
        JOIN order_stats o ON r.merchant_id = o.merchant_id
        WHERE o.total_orders >= 10  -- Minimum orders for statistical significance
        AND (r.orders_refunded::float / o.total_orders * 100) > :threshold
        ORDER BY refund_rate DESC
        """
        
        results = await database.fetch_all(query, {
            "since_date": since_date,
            "threshold": threshold_percent
        })
        
        return [
            {
                "merchant_id": r["merchant_id"],
                "orders_refunded": r["orders_refunded"],
                "total_orders": r["total_orders"],
                "refund_rate_percent": round(float(r["refund_rate"]), 2),
                "total_refunded": float(r["total_refunded"])
            }
            for r in results
        ]
    
    @staticmethod
    async def get_failed_refunds_needing_retry() -> list[Dict[str, Any]]:
        """Get failed refunds that need attention"""
        query = """
        SELECT 
            r.refund_id,
            r.order_id,
            r.merchant_id,
            r.amount,
            r.error_message,
            r.created_at,
            q.retry_count,
            q.last_error
        FROM refund_records r
        LEFT JOIN refund_retry_queue q ON r.refund_id = q.refund_id
        WHERE r.status = 'failed'
        AND (q.retry_count >= q.max_retries OR q.retry_count IS NULL)
        ORDER BY r.created_at DESC
        LIMIT 50
        """
        
        results = await database.fetch_all(query)
        
        return [dict(r) for r in results]


# API endpoints for metrics
from fastapi import APIRouter, Depends
from utils.auth import get_current_user, require_admin

metrics_router = APIRouter(prefix="/metrics/refunds", tags=["refund-metrics"])


@metrics_router.get("/stats")
async def get_refund_metrics(
    days: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get refund statistics for the current merchant"""
    merchant_id = current_user.get("merchant_id")
    
    if not merchant_id:
        # Admin can view global stats
        if current_user.get("role") != "admin":
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Not authorized")
        merchant_id = None
    
    stats = await RefundMetrics.get_refund_stats(merchant_id, days)
    return {"status": "success", "data": stats}


@metrics_router.get("/breakdown/reason")
async def get_refund_reason_breakdown(
    days: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get refund breakdown by reason"""
    merchant_id = current_user.get("merchant_id")
    
    if not merchant_id and current_user.get("role") != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Not authorized")
    
    breakdown = await RefundMetrics.get_refund_breakdown_by_reason(merchant_id, days)
    return {"status": "success", "data": breakdown}


@metrics_router.get("/trend")
async def get_refund_daily_trend(
    days: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get daily refund trend"""
    merchant_id = current_user.get("merchant_id")
    
    if not merchant_id and current_user.get("role") != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Not authorized")
    
    trend = await RefundMetrics.get_refund_trend(merchant_id, days)
    return {"status": "success", "data": trend}


@metrics_router.get("/alerts/high-refund-rate")
async def get_high_refund_rate_alerts(
    threshold: float = 15.0,
    days: int = 30,
    current_user: dict = Depends(require_admin)
):
    """Get merchants with high refund rates (Admin only)"""
    merchants = await RefundMetrics.check_high_refund_rate_merchants(threshold, days)
    return {
        "status": "success",
        "threshold_percent": threshold,
        "period_days": days,
        "merchants": merchants,
        "count": len(merchants)
    }


@metrics_router.get("/alerts/failed-refunds")
async def get_failed_refunds_alert(
    current_user: dict = Depends(require_admin)
):
    """Get failed refunds needing attention (Admin only)"""
    failed_refunds = await RefundMetrics.get_failed_refunds_needing_retry()
    return {
        "status": "success",
        "failed_refunds": failed_refunds,
        "count": len(failed_refunds)
    }


# Export
refund_metrics = RefundMetrics()

