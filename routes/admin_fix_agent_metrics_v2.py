"""Admin endpoint to fix agent metrics calculation - use orders table instead of usage logs"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

# This module's whole body used to appear twice, the second copy rebinding `router`; the dead
# first copy is gone, so this is the one main.py mounts.
router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

# RETIRED (501). After ALTERing orders and swapping the agent_metrics_24h view (nothing reads it),
# it ran UPDATE agents SET total_requests = total_orders = <order count>, total_gmv,
# request_count, success_rate, last_used_at from orders joined through agent_merchants, for
# every agent with an order. agents has no request_count column (db/agents.py), so that UPDATE
# fails on prod's table on every call and cannot run there. Dropping only request_count would
# arm it: a bulk overwrite of the live counters, with total_requests redefined as an order count.
@router.post("/agent-metrics-v2")
async def fix_agent_metrics_v2(current_user: dict = Depends(get_current_user)):
    """Retired: answers 501 and writes nothing."""
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="The agent metrics v2 fix is retired; it would overwrite live agent counters",
    )

@router.get("/agent-orders-check")
async def check_agent_orders(
    agent_id: Optional[str] = Query('agent_ee38f2b3645a2ec2'),
    current_user: dict = Depends(get_current_user)
):
    """
    Check agent's actual orders from orders table
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can check orders"
        )
    
    try:
        # Check direct orders (if agent_id exists in orders table)
        direct_orders_query = """
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as total_gmv,
                COUNT(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 END) as paid_orders
            FROM orders
            WHERE agent_id = :agent_id
        """
        
        direct_orders = await database.fetch_one(direct_orders_query, {"agent_id": agent_id})
        
        # Check orders via merchant association
        via_merchant_query = """
            SELECT 
                COUNT(o.*) as total_orders,
                COALESCE(SUM(o.total), 0) as total_gmv,
                COUNT(CASE WHEN o.payment_status IN ('paid', 'completed', 'succeeded') THEN 1 END) as paid_orders
            FROM orders o
            JOIN agent_merchants am ON o.merchant_id = am.merchant_id
            WHERE am.agent_id = :agent_id
        """
        
        via_merchant = await database.fetch_one(via_merchant_query, {"agent_id": agent_id})
        
        # Check what's in agent_usage_logs (current wrong source)
        usage_logs_query = """
            SELECT 
                COUNT(*) as total_logs,
                COUNT(DISTINCT order_id) as unique_orders,
                COALESCE(SUM(order_amount), 0) as total_gmv
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
        """
        
        usage_logs = await database.fetch_one(usage_logs_query, {"agent_id": agent_id})
        
        # Check current agent stats (agents has no request_count column; reading it 500'd this)
        agent_stats_query = """
            SELECT 
                total_requests,
                total_orders,
                total_gmv,
                success_rate
            FROM agents
            WHERE agent_id = :agent_id
        """
        
        agent_stats = await database.fetch_one(agent_stats_query, {"agent_id": agent_id})
        
        return {
            "agent_id": agent_id,
            "direct_orders": dict(direct_orders) if direct_orders else {},
            "orders_via_merchant": dict(via_merchant) if via_merchant else {},
            "usage_logs_data": dict(usage_logs) if usage_logs else {},
            "current_agent_stats": dict(agent_stats) if agent_stats else {},
            "recommendation": "Should use orders_via_merchant data for agent metrics"
        }
        
    except Exception as e:
        logger.error(f"Error checking agent orders: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check orders: {str(e)}"
        )

