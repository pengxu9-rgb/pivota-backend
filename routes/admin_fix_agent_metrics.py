"""Admin endpoint to fix agent metrics display"""
from fastapi import APIRouter, Depends, HTTPException, status
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
import logging
from typing import Dict, Any

router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

@router.post("/agent-metrics")
async def fix_agent_metrics(current_user: dict = Depends(get_current_user)):
    """
    Fix agent metrics by creating the agent_metrics_24h view if it doesn't exist
    and ensuring data is properly calculated
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    try:
        # Drop existing view if it exists
        await database.execute("DROP VIEW IF EXISTS agent_metrics_24h CASCADE")
        
        # Create the agent_metrics_24h view to calculate 24h metrics from usage logs
        create_view_query = """
            CREATE VIEW agent_metrics_24h AS
            SELECT 
                al.agent_id,
                COUNT(*) as requests_24h,
                COUNT(CASE WHEN al.status_code BETWEEN 200 AND 299 THEN 1 END) as successful_24h,
                COUNT(CASE WHEN al.status_code >= 400 THEN 1 END) as failed_24h,
                CASE 
                    WHEN COUNT(*) > 0 THEN 
                        (COUNT(CASE WHEN al.status_code BETWEEN 200 AND 299 THEN 1 END)::FLOAT / COUNT(*)::FLOAT * 100)
                    ELSE 0 
                END as success_rate_24h,
                COALESCE(AVG(al.response_time_ms), 0) as avg_latency_24h,
                COUNT(DISTINCT al.order_id) FILTER (WHERE al.order_id IS NOT NULL) as orders_24h,
                COALESCE(SUM(al.order_amount), 0) as gmv_24h
            FROM agent_usage_logs al
            WHERE al.timestamp >= NOW() - INTERVAL '24 hours'
            GROUP BY al.agent_id
        """
        
        await database.execute(create_view_query)
        logger.info("✅ Created agent_metrics_24h view")
        
        # Also update agents table to sync request_count with total_requests
        sync_query = """
            UPDATE agents 
            SET 
                request_count = total_requests,
                success_rate = CASE 
                    WHEN total_requests > 0 THEN 
                        (SELECT COUNT(*)::FLOAT / total_requests::FLOAT * 100 
                         FROM agent_usage_logs 
                         WHERE agent_id = agents.agent_id 
                         AND status_code BETWEEN 200 AND 299)
                    ELSE 0 
                END
            WHERE total_requests > 0
        """
        
        result = await database.execute(sync_query)
        
        # Get current metrics for verification
        check_query = """
            SELECT 
                a.agent_id,
                a.name,
                a.total_requests,
                a.request_count,
                a.total_orders,
                a.total_gmv,
                m.requests_24h,
                m.orders_24h,
                m.gmv_24h
            FROM agents a
            LEFT JOIN agent_metrics_24h m ON a.agent_id = m.agent_id
            WHERE a.total_requests > 0
            LIMIT 5
        """
        
        samples = await database.fetch_all(check_query)
        
        return {
            "success": True,
            "message": "Agent metrics view created and data synced",
            "view_created": "agent_metrics_24h",
            "agents_updated": result if result else 0,
            "sample_data": [dict(s) for s in samples] if samples else []
        }
        
    except Exception as e:
        logger.error(f"Error fixing agent metrics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agent metrics: {str(e)}"
        )

@router.get("/agent-metrics-status")
async def check_agent_metrics_status(current_user: dict = Depends(get_current_user)):
    """
    Check the current status of agent metrics
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check metrics status"
        )
    
    try:
        # Check if view exists
        view_check = """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views 
                WHERE table_name = 'agent_metrics_24h'
            ) as view_exists
        """
        
        view_result = await database.fetch_one(view_check)
        
        # Check agent data discrepancies
        discrepancy_check = """
            SELECT 
                COUNT(*) as total_agents,
                COUNT(CASE WHEN total_requests > 0 AND request_count = 0 THEN 1 END) as agents_with_discrepancy,
                SUM(total_requests) as total_all_requests,
                SUM(request_count) as total_request_count
            FROM agents
        """
        
        discrepancy = await database.fetch_one(discrepancy_check)
        
        # Check usage logs
        usage_check = """
            SELECT 
                COUNT(DISTINCT agent_id) as agents_with_logs,
                COUNT(*) as total_logs,
                MIN(timestamp) as earliest_log,
                MAX(timestamp) as latest_log
            FROM agent_usage_logs
        """
        
        usage = await database.fetch_one(usage_check)
        
        return {
            "view_exists": view_result["view_exists"],
            "agents": {
                "total": discrepancy["total_agents"],
                "with_discrepancy": discrepancy["agents_with_discrepancy"],
                "total_requests_sum": discrepancy["total_all_requests"],
                "request_count_sum": discrepancy["total_request_count"]
            },
            "usage_logs": {
                "agents_with_logs": usage["agents_with_logs"],
                "total_logs": usage["total_logs"],
                "earliest": usage["earliest_log"].isoformat() if usage["earliest_log"] else None,
                "latest": usage["latest_log"].isoformat() if usage["latest_log"] else None
            },
            "needs_fix": not view_result["view_exists"] or discrepancy["agents_with_discrepancy"] > 0
        }
        
    except Exception as e:
        logger.error(f"Error checking metrics status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check metrics status: {str(e)}"
        )

from fastapi import APIRouter, Depends, HTTPException, status
from db.database import database
from utils.auth import get_current_user
import logging
from typing import Dict, Any

router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

@router.post("/agent-metrics")
async def fix_agent_metrics(current_user: dict = Depends(get_current_user)):
    """
    Fix agent metrics by creating the agent_metrics_24h view if it doesn't exist
    and ensuring data is properly calculated
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    try:
        # Drop existing view if it exists
        await database.execute("DROP VIEW IF EXISTS agent_metrics_24h CASCADE")
        
        # Create the agent_metrics_24h view to calculate 24h metrics from usage logs
        create_view_query = """
            CREATE VIEW agent_metrics_24h AS
            SELECT 
                al.agent_id,
                COUNT(*) as requests_24h,
                COUNT(CASE WHEN al.status_code BETWEEN 200 AND 299 THEN 1 END) as successful_24h,
                COUNT(CASE WHEN al.status_code >= 400 THEN 1 END) as failed_24h,
                CASE 
                    WHEN COUNT(*) > 0 THEN 
                        (COUNT(CASE WHEN al.status_code BETWEEN 200 AND 299 THEN 1 END)::FLOAT / COUNT(*)::FLOAT * 100)
                    ELSE 0 
                END as success_rate_24h,
                COALESCE(AVG(al.response_time_ms), 0) as avg_latency_24h,
                COUNT(DISTINCT al.order_id) FILTER (WHERE al.order_id IS NOT NULL) as orders_24h,
                COALESCE(SUM(al.order_amount), 0) as gmv_24h
            FROM agent_usage_logs al
            WHERE al.timestamp >= NOW() - INTERVAL '24 hours'
            GROUP BY al.agent_id
        """
        
        await database.execute(create_view_query)
        logger.info("✅ Created agent_metrics_24h view")
        
        # Also update agents table to sync request_count with total_requests
        sync_query = """
            UPDATE agents 
            SET 
                request_count = total_requests,
                success_rate = CASE 
                    WHEN total_requests > 0 THEN 
                        (SELECT COUNT(*)::FLOAT / total_requests::FLOAT * 100 
                         FROM agent_usage_logs 
                         WHERE agent_id = agents.agent_id 
                         AND status_code BETWEEN 200 AND 299)
                    ELSE 0 
                END
            WHERE total_requests > 0
        """
        
        result = await database.execute(sync_query)
        
        # Get current metrics for verification
        check_query = """
            SELECT 
                a.agent_id,
                a.name,
                a.total_requests,
                a.request_count,
                a.total_orders,
                a.total_gmv,
                m.requests_24h,
                m.orders_24h,
                m.gmv_24h
            FROM agents a
            LEFT JOIN agent_metrics_24h m ON a.agent_id = m.agent_id
            WHERE a.total_requests > 0
            LIMIT 5
        """
        
        samples = await database.fetch_all(check_query)
        
        return {
            "success": True,
            "message": "Agent metrics view created and data synced",
            "view_created": "agent_metrics_24h",
            "agents_updated": result if result else 0,
            "sample_data": [dict(s) for s in samples] if samples else []
        }
        
    except Exception as e:
        logger.error(f"Error fixing agent metrics: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agent metrics: {str(e)}"
        )

@router.get("/agent-metrics-status")
async def check_agent_metrics_status(current_user: dict = Depends(get_current_user)):
    """
    Check the current status of agent metrics
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check metrics status"
        )
    
    try:
        # Check if view exists
        view_check = """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views 
                WHERE table_name = 'agent_metrics_24h'
            ) as view_exists
        """
        
        view_result = await database.fetch_one(view_check)
        
        # Check agent data discrepancies
        discrepancy_check = """
            SELECT 
                COUNT(*) as total_agents,
                COUNT(CASE WHEN total_requests > 0 AND request_count = 0 THEN 1 END) as agents_with_discrepancy,
                SUM(total_requests) as total_all_requests,
                SUM(request_count) as total_request_count
            FROM agents
        """
        
        discrepancy = await database.fetch_one(discrepancy_check)
        
        # Check usage logs
        usage_check = """
            SELECT 
                COUNT(DISTINCT agent_id) as agents_with_logs,
                COUNT(*) as total_logs,
                MIN(timestamp) as earliest_log,
                MAX(timestamp) as latest_log
            FROM agent_usage_logs
        """
        
        usage = await database.fetch_one(usage_check)
        
        return {
            "view_exists": view_result["view_exists"],
            "agents": {
                "total": discrepancy["total_agents"],
                "with_discrepancy": discrepancy["agents_with_discrepancy"],
                "total_requests_sum": discrepancy["total_all_requests"],
                "request_count_sum": discrepancy["total_request_count"]
            },
            "usage_logs": {
                "agents_with_logs": usage["agents_with_logs"],
                "total_logs": usage["total_logs"],
                "earliest": usage["earliest_log"].isoformat() if usage["earliest_log"] else None,
                "latest": usage["latest_log"].isoformat() if usage["latest_log"] else None
            },
            "needs_fix": not view_result["view_exists"] or discrepancy["agents_with_discrepancy"] > 0
        }
        
    except Exception as e:
        logger.error(f"Error checking metrics status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check metrics status: {str(e)}"
        )

