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
    time_range: str = Query("today", description="Time range: today, week, month")
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
        # Infer PSP from payment_intent_id prefix since orders table doesn't have psp_type
        query = """
        WITH psp_stats AS (
            SELECT 
                mp.provider as psp_name,
                mp.status,
                COUNT(DISTINCT mp.merchant_id) as merchant_count,
                COALESCE(COUNT(CASE 
                    WHEN mp.provider = 'stripe' AND o.payment_intent_id LIKE 'pi_%' THEN o.order_id
                    WHEN mp.provider = 'paypal' AND o.payment_intent_id ~ '^[A-Z0-9]+$' AND o.payment_intent_id NOT LIKE 'pi_%' AND o.payment_intent_id NOT LIKE 'chk_%' THEN o.order_id
                    WHEN mp.provider = 'checkout' AND o.payment_intent_id LIKE 'chk_%' THEN o.order_id
                    WHEN mp.provider = 'adyen' AND o.payment_intent_id IS NULL AND o.payment_status = 'paid' THEN o.order_id
                END), 0) as transaction_count,
                COALESCE(COUNT(CASE 
                    WHEN o.payment_status = 'paid' AND (
                        (mp.provider = 'stripe' AND o.payment_intent_id LIKE 'pi_%') OR
                        (mp.provider = 'paypal' AND o.payment_intent_id ~ '^[A-Z0-9]+$' AND o.payment_intent_id NOT LIKE 'pi_%') OR
                        (mp.provider = 'checkout' AND o.payment_intent_id LIKE 'chk_%') OR
                        (mp.provider = 'adyen' AND o.payment_intent_id IS NULL)
                    ) THEN 1
                END), 0) as success_count,
                COALESCE(SUM(CASE 
                    WHEN o.payment_status = 'paid' AND (
                        (mp.provider = 'stripe' AND o.payment_intent_id LIKE 'pi_%') OR
                        (mp.provider = 'paypal' AND o.payment_intent_id ~ '^[A-Z0-9]+$' AND o.payment_intent_id NOT LIKE 'pi_%') OR
                        (mp.provider = 'checkout' AND o.payment_intent_id LIKE 'chk_%') OR
                        (mp.provider = 'adyen' AND o.payment_intent_id IS NULL)
                    ) THEN o.total ELSE 0
                END), 0) as total_volume,
                AVG(CASE 
                    WHEN o.payment_status = 'paid' AND (
                        (mp.provider = 'stripe' AND o.payment_intent_id LIKE 'pi_%') OR
                        (mp.provider = 'paypal' AND o.payment_intent_id ~ '^[A-Z0-9]+$' AND o.payment_intent_id NOT LIKE 'pi_%') OR
                        (mp.provider = 'checkout' AND o.payment_intent_id LIKE 'chk_%') OR
                        (mp.provider = 'adyen' AND o.payment_intent_id IS NULL)
                    ) THEN o.total ELSE NULL
                END) as avg_transaction_size,
                COALESCE(COUNT(CASE WHEN o.payment_status IN ('refunded', 'partially_refunded') THEN 1 END), 0) as refund_count,
                MAX(CASE 
                    WHEN (
                        (mp.provider = 'stripe' AND o.payment_intent_id LIKE 'pi_%') OR
                        (mp.provider = 'paypal' AND o.payment_intent_id ~ '^[A-Z0-9]+$' AND o.payment_intent_id NOT LIKE 'pi_%') OR
                        (mp.provider = 'checkout' AND o.payment_intent_id LIKE 'chk_%') OR
                        (mp.provider = 'adyen' AND o.payment_intent_id IS NULL)
                    ) THEN o.created_at ELSE NULL
                END) as last_transaction
            FROM merchant_psps mp
            LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
                AND o.created_at >= :start_time
            WHERE mp.status = 'active'
            GROUP BY mp.provider, mp.status
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
            ps.psp_name,
            ps.status,
            ps.merchant_count,
            COALESCE(ps.transaction_count, 0) as transactions_today,
            COALESCE(ps.success_count, 0) as successful_transactions,
            CASE 
                WHEN ps.transaction_count > 0 
                THEN ROUND(ps.success_count::numeric / ps.transaction_count * 100, 2)
                ELSE 0 
            END as success_rate,
            COALESCE(ps.total_volume, 0) as total_volume,
            COALESCE(ps.avg_transaction_size, 0) as avg_transaction_size,
            COALESCE(ps.refund_count, 0) as refund_count,
            fs.avg_fee_rate,
            ps.last_transaction,
            CASE 
                WHEN ps.last_transaction IS NULL THEN 'Never'
                WHEN ps.last_transaction > NOW() - INTERVAL '10 minutes' THEN 'Active'
                WHEN ps.last_transaction > NOW() - INTERVAL '1 hour' THEN 'Recently Active'
                ELSE 'Inactive'
            END as activity_status
        FROM psp_stats ps
        LEFT JOIN fee_stats fs ON ps.psp_name = fs.provider
        ORDER BY ps.transaction_count DESC NULLS LAST
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
        
        return {
            "psps": psp_list,
            "summary": {
                "total_psps": len(psp_list),
                "healthy_psps": len([p for p in psp_list if p["health_status"] == "healthy"]),
                "degraded_psps": len([p for p in psp_list if p["health_status"] == "degraded"]),
                "critical_psps": len([p for p in psp_list if p["health_status"] == "critical"]),
                "total_transactions": sum(p["transactions_today"] for p in psp_list),
                "total_volume": sum(p["total_volume"] for p in psp_list),
                "avg_success_rate": round(sum(p["success_rate"] for p in psp_list) / len(psp_list), 2) if psp_list else 0
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
        
        # Get aggregated metrics
        # Note: Using merchant_id to link orders to PSP, not psp_type
        metrics_query = """
        SELECT 
            COUNT(o.order_id) as total_transactions,
            COUNT(CASE WHEN o.payment_status = 'paid' THEN 1 END) as successful_transactions,
            COUNT(CASE WHEN o.payment_status = 'failed' THEN 1 END) as failed_transactions,
            COUNT(CASE WHEN o.payment_status = 'pending' THEN 1 END) as pending_transactions,
            COUNT(CASE WHEN o.payment_status IN ('refunded', 'partially_refunded') THEN 1 END) as refunded_transactions,
            SUM(CASE WHEN o.payment_status = 'paid' THEN o.total ELSE 0 END) as total_volume,
            AVG(CASE WHEN o.payment_status = 'paid' THEN o.total ELSE NULL END) as avg_transaction_size,
            MIN(o.total) as min_transaction_size,
            MAX(o.total) as max_transaction_size
        FROM orders o
        JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
        WHERE mp.provider = :psp_id 
            AND mp.status = 'active'
            AND o.created_at >= :start_time
        """
        
        metrics = await database.fetch_one(metrics_query, {
            "psp_id": psp_id,
            "start_time": start_time
        })
        
        # Get merchant breakdown
        merchant_query = """
        SELECT 
            m.merchant_id,
            m.business_name as merchant_name,
            COUNT(o.order_id) as transaction_count,
            COUNT(CASE WHEN o.payment_status = 'paid' THEN 1 END) as success_count,
            COUNT(CASE WHEN o.payment_status IN ('refunded', 'partially_refunded') THEN 1 END) as refund_count,
            SUM(CASE WHEN o.payment_status = 'paid' THEN o.total ELSE 0 END) as volume,
            MAX(o.created_at) as last_transaction
        FROM merchant_onboarding m
        JOIN merchant_psps mp ON m.merchant_id = mp.merchant_id
        LEFT JOIN orders o ON o.merchant_id = m.merchant_id 
            AND o.created_at >= :start_time
        WHERE mp.provider = :psp_id AND mp.status = 'active'
        GROUP BY m.merchant_id, m.business_name
        ORDER BY volume DESC
        LIMIT 50
        """
        
        merchants = await database.fetch_all(merchant_query, {
            "psp_id": psp_id,
            "start_time": start_time
        })
        
        # Get hourly trend data (last 24 hours)
        trend_query = """
        SELECT 
            DATE_TRUNC('hour', o.created_at) as hour,
            COUNT(o.order_id) as transactions,
            COUNT(CASE WHEN o.payment_status = 'paid' THEN 1 END) as successful,
            SUM(CASE WHEN o.payment_status = 'paid' THEN o.total ELSE 0 END) as volume
        FROM orders o
        JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
        WHERE mp.provider = :psp_id 
            AND mp.status = 'active'
            AND o.created_at >= NOW() - INTERVAL '24 hours'
        GROUP BY hour
        ORDER BY hour DESC
        """
        
        trends = await database.fetch_all(trend_query, {"psp_id": psp_id})
        
        # Calculate metrics
        total_transactions = metrics["total_transactions"] or 0
        successful_transactions = metrics["successful_transactions"] or 0
        success_rate = round(successful_transactions / total_transactions * 100, 2) if total_transactions > 0 else 0
        
        # Format merchant stats
        merchant_stats = []
        for merchant in merchants:
            tx_count = merchant["transaction_count"] or 0
            success_count = merchant["success_count"] or 0
            refund_count = merchant["refund_count"] or 0
            
            merchant_stats.append({
                "merchant_id": merchant["merchant_id"],
                "merchant_name": merchant["merchant_name"] or "Unknown",
                "volume": float(merchant["volume"]) if merchant["volume"] else 0,
                "transaction_count": tx_count,
                "success_rate": round(success_count / tx_count * 100, 2) if tx_count > 0 else 0,
                "refund_rate": round(refund_count / tx_count * 100, 2) if tx_count > 0 else 0,
                "last_tx": merchant["last_transaction"].isoformat() if merchant["last_transaction"] else None
            })
        
        # Format trend data
        trend_data = []
        for trend in trends:
            trend_data.append({
                "timestamp": trend["hour"].isoformat(),
                "transactions": trend["transactions"],
                "successful": trend["successful"],
                "volume": float(trend["volume"]) if trend["volume"] else 0,
                "success_rate": round(trend["successful"] / trend["transactions"] * 100, 2) if trend["transactions"] > 0 else 0
            })
        
        return {
            "psp_id": psp_id,
            "name": psp_id.capitalize(),
            "status": "active",
            "merchant_count": psp_info["merchant_count"],
            "first_connected": psp_info["first_connected"].isoformat() if psp_info["first_connected"] else None,
            "last_connected": psp_info["last_connected"].isoformat() if psp_info["last_connected"] else None,
            "metrics": {
                "total_transactions": total_transactions,
                "successful_transactions": successful_transactions,
                "failed_transactions": metrics["failed_transactions"] or 0,
                "pending_transactions": metrics["pending_transactions"] or 0,
                "refunded_transactions": metrics["refunded_transactions"] or 0,
                "success_rate": success_rate,
                "total_volume": float(metrics["total_volume"]) if metrics["total_volume"] else 0,
                "avg_transaction_size": float(metrics["avg_transaction_size"]) if metrics["avg_transaction_size"] else 0,
                "min_transaction_size": float(metrics["min_transaction_size"]) if metrics["min_transaction_size"] else 0,
                "max_transaction_size": float(metrics["max_transaction_size"]) if metrics["max_transaction_size"] else 0
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
