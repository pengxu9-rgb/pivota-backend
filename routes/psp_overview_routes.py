"""
PSP Overview Routes for Employee Dashboard
Provides aggregated PSP metrics and health monitoring
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from db.database import database
from routes.auth_routes import require_employee
from sqlalchemy import text

router = APIRouter(prefix="/api/psp", tags=["PSP Overview"])
logger = logging.getLogger(__name__)

@router.get("/overview")
async def get_psp_overview(
    current_user: dict = Depends(require_employee),
    time_range: str = Query("week", description="Time range: today, week, month")
):
    """
    Get overview of all PSPs with aggregated metrics
    """
    try:
        # Calculate time range
        now = datetime.utcnow()
        if time_range == "today":
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif time_range == "week":
            start_time = now - timedelta(days=7)
        elif time_range == "month":
            start_time = now - timedelta(days=30)
        else:
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Get PSP configurations and their stats
        # For the employee PSP overview we want an **attempt-level**
        # view based on the payment_attempts table, not on final
        # orders. Each payment_attempt row represents a concrete call
        # to a PSP (including failover attempts).
        query = """
        WITH psp_configs AS (
            SELECT 
                LOWER(provider) AS psp_key,
                provider AS psp_name,
                -- A PSP is considered active if any linked merchant PSP record is active
                CASE 
                    WHEN BOOL_OR(status = 'active') THEN 'active'
                    ELSE MAX(status)
                END AS status,
                COUNT(DISTINCT merchant_id) FILTER (WHERE status = 'active') AS merchant_count
            FROM merchant_psps
            GROUP BY LOWER(provider), provider
        ),
        attempt_stats AS (
            SELECT 
                LOWER(psp_name) AS psp_key,
                COUNT(*) AS total_attempts,
                COUNT(*) FILTER (WHERE status = 'success') AS successful_attempts,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed_attempts,
                COUNT(*) FILTER (WHERE status = 'timeout') AS timeout_attempts,
                COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled_attempts,
                SUM(CASE WHEN status = 'success' THEN amount ELSE 0 END) AS total_volume,
                AVG(CASE WHEN status = 'success' THEN amount ELSE NULL END) AS avg_transaction_size,
                MAX(created_at) AS last_attempt
            FROM payment_attempts
            WHERE created_at >= :start_time
            GROUP BY LOWER(psp_name)
        ),
        fee_stats AS (
            SELECT 
                provider,
                AVG(
                    CASE 
                        WHEN provider = 'stripe' THEN 2.9
                        WHEN provider = 'adyen' THEN 2.0
                        WHEN provider = 'checkout' THEN 2.4
                        WHEN provider = 'paypal' THEN 3.1
                        ELSE 2.5
                    END
                ) as avg_fee_rate
            FROM merchant_psps
            WHERE status = 'active'
            GROUP BY provider
        )
        SELECT 
            pc.psp_name,
            pc.status,
            pc.merchant_count,
            COALESCE(a.total_attempts, 0) AS transactions_today,
            COALESCE(a.successful_attempts, 0) AS successful_transactions,
            CASE 
                WHEN COALESCE(a.total_attempts, 0) > 0 
                THEN ROUND(a.successful_attempts::numeric / NULLIF(a.total_attempts, 0) * 100, 2)
                ELSE 0 
            END AS success_rate,
            COALESCE(a.total_volume, 0) AS total_volume,
            COALESCE(a.avg_transaction_size, 0) AS avg_transaction_size,
            -- We don't yet track refunds at attempt level; expose 0 for now
            0 AS refund_count,
            fs.avg_fee_rate,
            a.last_attempt,
            CASE 
                WHEN a.last_attempt IS NULL THEN 'Never'
                WHEN a.last_attempt > NOW() - INTERVAL '10 minutes' THEN 'Active'
                WHEN a.last_attempt > NOW() - INTERVAL '1 hour' THEN 'Recently Active'
                ELSE 'Inactive'
            END AS activity_status
        FROM psp_configs pc
        LEFT JOIN attempt_stats a ON a.psp_key = pc.psp_key
        LEFT JOIN fee_stats fs ON pc.psp_name = fs.provider
        WHERE pc.status = 'active'
        ORDER BY transactions_today DESC NULLS LAST
        """
        
        results = await database.fetch_all(query, {"start_time": start_time})
        
        # Format response
        psp_list = []
        for row in results:
            # Calculate health score based on success rate and activity
            health_score = 100
            success_rate = float(row["success_rate"])
            
            if success_rate < 90:
                health_score -= 30
            elif success_rate < 95:
                health_score -= 15
            elif success_rate < 98:
                health_score -= 5
                
            if row["activity_status"] == "Inactive":
                health_score -= 20
            elif row["activity_status"] == "Recently Active":
                health_score -= 10
                
            # Determine status based on health score
            if health_score >= 90:
                health_status = "healthy"
            elif health_score >= 70:
                health_status = "degraded"
            else:
                health_status = "critical"
            
            psp_list.append({
                "psp_id": row["psp_name"].lower(),
                "name": row["psp_name"].capitalize(),
                "status": row["status"],
                "health_status": health_status,
                "health_score": health_score,
                "merchant_count": row["merchant_count"],
                "transactions_today": row["transactions_today"],
                "successful_transactions": row["successful_transactions"],
                "success_rate": success_rate,
                "total_volume": float(row["total_volume"]) if row["total_volume"] else 0,
                "avg_transaction_size": float(row["avg_transaction_size"]) if row["avg_transaction_size"] else 0,
                "refund_count": row["refund_count"],
                "refund_rate": round(row["refund_count"] / row["transactions_today"] * 100, 2) if row["transactions_today"] > 0 else 0,
                "avg_fee": float(row["avg_fee_rate"]) if row["avg_fee_rate"] else 2.5,
                "last_synced": row["last_transaction"].isoformat() if row["last_transaction"] else None,
                "activity_status": row["activity_status"]
            })
        
        # Calculate weighted average success rate (by transaction volume)
        total_transactions = sum(p["transactions_today"] for p in psp_list)
        if total_transactions > 0:
            # Weighted average: (success_rate * transactions) / total_transactions
            weighted_success = sum(
                p["success_rate"] * p["transactions_today"] 
                for p in psp_list
            ) / total_transactions
            avg_success_rate = round(weighted_success, 2)
        else:
            # No transactions - calculate simple average of PSPs with transactions
            psps_with_txns = [p for p in psp_list if p["transactions_today"] > 0]
            avg_success_rate = round(
                sum(p["success_rate"] for p in psps_with_txns) / len(psps_with_txns), 2
            ) if psps_with_txns else 0
        
        return {
            "psps": psp_list,
            "summary": {
                "total_psps": len(psp_list),
                "healthy_psps": len([p for p in psp_list if p["health_status"] == "healthy"]),
                "degraded_psps": len([p for p in psp_list if p["health_status"] == "degraded"]),
                "critical_psps": len([p for p in psp_list if p["health_status"] == "critical"]),
                "total_transactions": total_transactions,
                "total_volume": sum(p["total_volume"] for p in psp_list),
                "avg_success_rate": avg_success_rate  # Now weighted by transaction volume
            },
            "time_range": time_range,
            "generated_at": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error fetching PSP overview: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch PSP overview: {str(e)}")


@router.get("/{psp_id}/detail")
async def get_psp_detail(
    psp_id: str,
    current_user: dict = Depends(require_employee),
    time_range: str = Query("today", description="Time range: today, week, month")
):
    """
    Get detailed metrics for a specific PSP including merchant breakdown
    """
    try:
        # Calculate time range
        now = datetime.utcnow()
        if time_range == "today":
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif time_range == "week":
            start_time = now - timedelta(days=7)
        elif time_range == "month":
            start_time = now - timedelta(days=30)
        else:
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Get PSP details
        psp_query = """
        SELECT 
            provider,
            COUNT(DISTINCT merchant_id) as merchant_count,
            MIN(connected_at) as first_connected,
            MAX(connected_at) as last_connected
        FROM merchant_psps
        WHERE provider = :psp_id AND status = 'active'
        GROUP BY provider
        """
        
        psp_info = await database.fetch_one(psp_query, {"psp_id": psp_id})
        
        if not psp_info:
            raise HTTPException(status_code=404, detail=f"PSP {psp_id} not found")
        
        # Get aggregated metrics based on payment_attempts (attempt-level view)
        metrics_query = """
        SELECT 
            COUNT(pa.attempt_id) as total_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'success') as successful_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'failed') as failed_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'timeout') as timeout_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'cancelled') as cancelled_attempts,
            COUNT(DISTINCT pa.order_id) as orders_touched,
            COUNT(DISTINCT pa.order_id) FILTER (WHERE pa.status = 'success') as orders_succeeded,
            SUM(CASE WHEN pa.status = 'success' THEN pa.amount ELSE 0 END) as total_volume,
            AVG(CASE WHEN pa.status = 'success' THEN pa.amount ELSE NULL END) as avg_amount,
            MIN(pa.amount) as min_amount,
            MAX(pa.amount) as max_amount
        FROM payment_attempts pa
        WHERE LOWER(pa.psp_name) = LOWER(:psp_id)
            AND pa.created_at >= :start_time
        """
        
        metrics = await database.fetch_one(metrics_query, {
            "psp_id": psp_id,
            "start_time": start_time
        }) or {}
        
        # Get merchant breakdown (per-merchant attempt metrics)
        merchant_query = """
        SELECT 
            m.merchant_id,
            m.business_name as merchant_name,
            COUNT(pa.attempt_id) as total_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'success') as successful_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'failed') as failed_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'timeout') as timeout_attempts,
            COUNT(DISTINCT pa.order_id) as orders_touched,
            COUNT(DISTINCT pa.order_id) FILTER (WHERE pa.status = 'success') as orders_succeeded,
            SUM(CASE WHEN pa.status = 'success' THEN pa.amount ELSE 0 END) as volume,
            MAX(pa.created_at) as last_attempt
        FROM merchant_onboarding m
        JOIN merchant_psps mp ON m.merchant_id = mp.merchant_id
        JOIN orders o ON o.merchant_id = m.merchant_id
            AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
        JOIN payment_attempts pa ON pa.order_id = o.order_id
        WHERE mp.provider = :psp_id 
            AND mp.status = 'active'
            AND LOWER(pa.psp_name) = LOWER(:psp_id)
            AND pa.created_at >= :start_time
        GROUP BY m.merchant_id, m.business_name
        ORDER BY volume DESC
        LIMIT 50
        """
        
        merchants = await database.fetch_all(merchant_query, {
            "psp_id": psp_id,
            "start_time": start_time
        })
        
        # Get hourly trend data (last 24 hours) based on attempts
        trend_query = """
        SELECT 
            DATE_TRUNC('hour', pa.created_at) as hour,
            COUNT(pa.attempt_id) as total_attempts,
            COUNT(*) FILTER (WHERE pa.status = 'success') as successful_attempts,
            SUM(CASE WHEN pa.status = 'success' THEN pa.amount ELSE 0 END) as volume
        FROM payment_attempts pa
        WHERE LOWER(pa.psp_name) = LOWER(:psp_id)
            AND pa.created_at >= NOW() - INTERVAL '24 hours'
        GROUP BY hour
        ORDER BY hour DESC
        """
        
        trends = await database.fetch_all(trend_query, {"psp_id": psp_id})
        
        # Calculate metrics
        total_attempts = metrics.get("total_attempts") or 0
        successful_attempts = metrics.get("successful_attempts") or 0
        success_rate = round(successful_attempts / total_attempts * 100, 2) if total_attempts > 0 else 0
        
        # Format merchant stats
        merchant_stats = []
        for merchant in merchants:
            attempts = merchant["total_attempts"] or 0
            successful = merchant["successful_attempts"] or 0
            
            merchant_stats.append({
                "merchant_id": merchant["merchant_id"],
                "merchant_name": merchant["merchant_name"] or "Unknown",
                "volume": float(merchant["volume"]) if merchant["volume"] else 0,
                "transaction_count": attempts,
                "success_rate": round(successful / attempts * 100, 2) if attempts > 0 else 0,
                "refund_rate": 0,
                "last_tx": merchant["last_attempt"].isoformat() if merchant["last_attempt"] else None
            })
        
        # Format trend data
        trend_data = []
        for trend in trends:
            total = trend["total_attempts"] or 0
            successful = trend["successful_attempts"] or 0
            trend_data.append({
                "timestamp": trend["hour"].isoformat(),
                "transactions": total,
                "successful": successful,
                "volume": float(trend["volume"]) if trend["volume"] else 0,
                "success_rate": round(successful / total * 100, 2) if total > 0 else 0
            })
        
        return {
            "psp_id": psp_id,
            "name": psp_id.capitalize(),
            "status": "active",
            "merchant_count": psp_info["merchant_count"],
            "first_connected": psp_info["first_connected"].isoformat() if psp_info["first_connected"] else None,
            "last_connected": psp_info["last_connected"].isoformat() if psp_info["last_connected"] else None,
            "metrics": {
                "total_transactions": total_attempts,
                "successful_transactions": successful_attempts,
                "failed_transactions": metrics.get("failed_attempts") or 0,
                "pending_transactions": (metrics.get("timeout_attempts") or 0) + (metrics.get("cancelled_attempts") or 0),
                "refunded_transactions": 0,
                "success_rate": success_rate,
                "total_volume": float(metrics.get("total_volume") or 0),
                "avg_transaction_size": float(metrics.get("avg_amount") or 0),
                "min_transaction_size": float(metrics.get("min_amount") or 0),
                "max_transaction_size": float(metrics.get("max_amount") or 0)
            },
            "merchant_stats": merchant_stats,
            "trend_data": trend_data,
            "time_range": time_range,
            "generated_at": datetime.utcnow().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching PSP detail for {psp_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch PSP detail: {str(e)}")


@router.post("/{psp_id}/sync")
async def sync_psp_data(
    psp_id: str,
    current_user: dict = Depends(require_employee)
):
    """
    Manually trigger PSP data synchronization
    """
    try:
        # In a real implementation, this would trigger a background job
        # to fetch latest data from the PSP's API
        
        # For now, we'll just update the last_synced timestamp
        await database.execute(
            """
            UPDATE merchant_psps 
            SET connected_at = NOW()
            WHERE provider = :psp_id AND status = 'active'
            """,
            {"psp_id": psp_id}
        )
        
        return {
            "status": "success",
            "message": f"Sync triggered for {psp_id}",
            "synced_at": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error syncing PSP {psp_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync PSP: {str(e)}")


@router.get("/{psp_id}/config")
async def get_psp_config(
    psp_id: str,
    current_user: dict = Depends(require_employee)
):
    """
    Get PSP configuration details (with masked sensitive data)
    """
    try:
        config_query = """
        SELECT 
            psp_id,
            provider,
            merchant_id,
            api_key,
            account_id,
            secret_key,
            status,
            capabilities,
            connected_at
        FROM merchant_psps
        WHERE provider = :psp_id AND status = 'active'
        LIMIT 1
        """
        
        config = await database.fetch_one(config_query, {"psp_id": psp_id})
        
        if not config:
            raise HTTPException(status_code=404, detail=f"PSP {psp_id} configuration not found")
        
        # Mask sensitive data
        def mask_key(key):
            if not key:
                return None
            if len(key) <= 8:
                return "*" * len(key)
            return key[:4] + "*" * (len(key) - 8) + key[-4:]
        
        return {
            "psp_id": config["psp_id"],
            "provider": config["provider"],
            "merchant_id": config["merchant_id"],
            "api_key_masked": mask_key(config["api_key"]),
            "account_id": config["account_id"],
            "secret_key_masked": mask_key(config["secret_key"]),
            "status": config["status"],
            "capabilities": config["capabilities"],
            "connected_at": config["connected_at"].isoformat() if config["connected_at"] else None,
            "routing_rules": {
                "preferred_regions": ["US", "EU", "APAC"],
                "fallback_to": ["stripe"] if psp_id != "stripe" else ["adyen"],
                "max_fee_rate": 3.5,
                "priority_weight": 0.8 if psp_id == "stripe" else 0.6
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching PSP config for {psp_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch PSP config: {str(e)}")
