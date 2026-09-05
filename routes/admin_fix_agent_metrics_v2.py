"""Admin endpoint to fix agent metrics calculation - use orders table instead of usage logs"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

@router.post("/agent-metrics-v2")
async def fix_agent_metrics_v2(current_user: dict = Depends(get_current_user)):
    """
    Fix agent metrics calculation to use orders table (like merchant does)
    instead of agent_usage_logs table
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    try:
        # First, check if orders table has agent_id column
        check_column = """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'orders' 
            AND column_name = 'agent_id'
        """
        has_agent_id = await database.fetch_one(check_column)
        
        if not has_agent_id:
            # Add agent_id column to orders table if it doesn't exist
            await database.execute("""
                ALTER TABLE orders 
                ADD COLUMN IF NOT EXISTS agent_id VARCHAR(50)
            """)
            
            # Create index for better performance
            await database.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_agent_id 
                ON orders(agent_id)
            """)
            logger.info("✅ Added agent_id column to orders table")
        
        # Drop old view if exists
        await database.execute("DROP VIEW IF EXISTS agent_metrics_24h CASCADE")
        
        # Create new view that calculates from orders table (like merchant does)
        create_view_query = """
            CREATE VIEW agent_metrics_24h AS
            SELECT 
                COALESCE(o.agent_id, am.agent_id) as agent_id,
                COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours') as requests_24h,
                COUNT(*) FILTER (WHERE o.payment_status IN ('paid', 'completed', 'succeeded') 
                    AND o.created_at >= NOW() - INTERVAL '24 hours') as successful_24h,
                COUNT(*) FILTER (WHERE o.payment_status IN ('failed', 'cancelled', 'error') 
                    AND o.created_at >= NOW() - INTERVAL '24 hours') as failed_24h,
                CASE 
                    WHEN COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours') > 0 THEN 
                        (COUNT(*) FILTER (WHERE o.payment_status IN ('paid', 'completed', 'succeeded') 
                            AND o.created_at >= NOW() - INTERVAL '24 hours')::FLOAT / 
                         COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours')::FLOAT * 100)
                    ELSE 0 
                END as success_rate_24h,
                0 as avg_latency_24h, -- Orders don't have latency
                COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours') as orders_24h,
                COALESCE(SUM(o.total) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours'), 0) as gmv_24h
            FROM orders o
            LEFT JOIN agent_merchants am ON o.merchant_id = am.merchant_id
            WHERE o.agent_id IS NOT NULL OR am.agent_id IS NOT NULL
            GROUP BY COALESCE(o.agent_id, am.agent_id)
        """
        
        await database.execute(create_view_query)
        logger.info("✅ Created agent_metrics_24h view based on orders table")
        
        # Update agents table to sync totals from orders
        sync_query = """
            UPDATE agents a
            SET 
                total_requests = subq.total_orders,
                total_orders = subq.total_orders,
                total_gmv = subq.total_gmv,
                request_count = subq.total_orders,
                success_rate = subq.success_rate,
                last_used_at = subq.last_order_date
            FROM (
                SELECT 
                    COALESCE(o.agent_id, am.agent_id) as agent_id,
                    COUNT(*) as total_orders,
                    COALESCE(SUM(o.total), 0) as total_gmv,
                    MAX(o.created_at) as last_order_date,
                    CASE 
                        WHEN COUNT(*) > 0 THEN 
                            (COUNT(*) FILTER (WHERE o.payment_status IN ('paid', 'completed', 'succeeded'))::FLOAT / 
                             COUNT(*)::FLOAT * 100)
                        ELSE 0 
                    END as success_rate
                FROM orders o
                LEFT JOIN agent_merchants am ON o.merchant_id = am.merchant_id
                WHERE o.agent_id IS NOT NULL OR am.agent_id IS NOT NULL
                GROUP BY COALESCE(o.agent_id, am.agent_id)
            ) subq
            WHERE a.agent_id = subq.agent_id
        """
        
        result = await database.execute(sync_query)
        
        # Get sample data for verification
        check_query = """
            SELECT 
                a.agent_id,
                a.name,
                a.total_requests,
                a.total_orders,
                a.total_gmv,
                m.requests_24h,
                m.orders_24h,
                m.gmv_24h
            FROM agents a
            LEFT JOIN agent_metrics_24h m ON a.agent_id = m.agent_id
            WHERE a.total_orders > 0 OR m.orders_24h > 0
            LIMIT 5
        """
        
        samples = await database.fetch_all(check_query)
        
        return {
            "success": True,
            "message": "Agent metrics now calculated from orders table (same as merchant)",
            "changes": {
                "view_created": "agent_metrics_24h (from orders table)",
                "agents_updated": result if result else 0,
                "calculation_method": "Now using orders table instead of agent_usage_logs"
            },
            "sample_data": [dict(s) for s in samples] if samples else []
        }
        
    except Exception as e:
        logger.error(f"Error fixing agent metrics v2: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agent metrics: {str(e)}"
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
        
        # Check current agent stats
        agent_stats_query = """
            SELECT 
                total_requests,
                request_count,
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

from fastapi import APIRouter, Depends, HTTPException, status, Query
from db.database import database
from utils.auth import get_current_user
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

router = APIRouter(prefix="/admin/fix", tags=["Admin Fix"])
logger = logging.getLogger(__name__)

@router.post("/agent-metrics-v2")
async def fix_agent_metrics_v2(current_user: dict = Depends(get_current_user)):
    """
    Fix agent metrics calculation to use orders table (like merchant does)
    instead of agent_usage_logs table
    """
    # Check if user is admin
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can execute this fix"
        )
    
    try:
        # First, check if orders table has agent_id column
        check_column = """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'orders' 
            AND column_name = 'agent_id'
        """
        has_agent_id = await database.fetch_one(check_column)
        
        if not has_agent_id:
            # Add agent_id column to orders table if it doesn't exist
            await database.execute("""
                ALTER TABLE orders 
                ADD COLUMN IF NOT EXISTS agent_id VARCHAR(50)
            """)
            
            # Create index for better performance
            await database.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_agent_id 
                ON orders(agent_id)
            """)
            logger.info("✅ Added agent_id column to orders table")
        
        # Drop old view if exists
        await database.execute("DROP VIEW IF EXISTS agent_metrics_24h CASCADE")
        
        # Create new view that calculates from orders table (like merchant does)
        create_view_query = """
            CREATE VIEW agent_metrics_24h AS
            SELECT 
                COALESCE(o.agent_id, am.agent_id) as agent_id,
                COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours') as requests_24h,
                COUNT(*) FILTER (WHERE o.payment_status IN ('paid', 'completed', 'succeeded') 
                    AND o.created_at >= NOW() - INTERVAL '24 hours') as successful_24h,
                COUNT(*) FILTER (WHERE o.payment_status IN ('failed', 'cancelled', 'error') 
                    AND o.created_at >= NOW() - INTERVAL '24 hours') as failed_24h,
                CASE 
                    WHEN COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours') > 0 THEN 
                        (COUNT(*) FILTER (WHERE o.payment_status IN ('paid', 'completed', 'succeeded') 
                            AND o.created_at >= NOW() - INTERVAL '24 hours')::FLOAT / 
                         COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours')::FLOAT * 100)
                    ELSE 0 
                END as success_rate_24h,
                0 as avg_latency_24h, -- Orders don't have latency
                COUNT(*) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours') as orders_24h,
                COALESCE(SUM(o.total) FILTER (WHERE o.created_at >= NOW() - INTERVAL '24 hours'), 0) as gmv_24h
            FROM orders o
            LEFT JOIN agent_merchants am ON o.merchant_id = am.merchant_id
            WHERE o.agent_id IS NOT NULL OR am.agent_id IS NOT NULL
            GROUP BY COALESCE(o.agent_id, am.agent_id)
        """
        
        await database.execute(create_view_query)
        logger.info("✅ Created agent_metrics_24h view based on orders table")
        
        # Update agents table to sync totals from orders
        sync_query = """
            UPDATE agents a
            SET 
                total_requests = subq.total_orders,
                total_orders = subq.total_orders,
                total_gmv = subq.total_gmv,
                request_count = subq.total_orders,
                success_rate = subq.success_rate,
                last_used_at = subq.last_order_date
            FROM (
                SELECT 
                    COALESCE(o.agent_id, am.agent_id) as agent_id,
                    COUNT(*) as total_orders,
                    COALESCE(SUM(o.total), 0) as total_gmv,
                    MAX(o.created_at) as last_order_date,
                    CASE 
                        WHEN COUNT(*) > 0 THEN 
                            (COUNT(*) FILTER (WHERE o.payment_status IN ('paid', 'completed', 'succeeded'))::FLOAT / 
                             COUNT(*)::FLOAT * 100)
                        ELSE 0 
                    END as success_rate
                FROM orders o
                LEFT JOIN agent_merchants am ON o.merchant_id = am.merchant_id
                WHERE o.agent_id IS NOT NULL OR am.agent_id IS NOT NULL
                GROUP BY COALESCE(o.agent_id, am.agent_id)
            ) subq
            WHERE a.agent_id = subq.agent_id
        """
        
        result = await database.execute(sync_query)
        
        # Get sample data for verification
        check_query = """
            SELECT 
                a.agent_id,
                a.name,
                a.total_requests,
                a.total_orders,
                a.total_gmv,
                m.requests_24h,
                m.orders_24h,
                m.gmv_24h
            FROM agents a
            LEFT JOIN agent_metrics_24h m ON a.agent_id = m.agent_id
            WHERE a.total_orders > 0 OR m.orders_24h > 0
            LIMIT 5
        """
        
        samples = await database.fetch_all(check_query)
        
        return {
            "success": True,
            "message": "Agent metrics now calculated from orders table (same as merchant)",
            "changes": {
                "view_created": "agent_metrics_24h (from orders table)",
                "agents_updated": result if result else 0,
                "calculation_method": "Now using orders table instead of agent_usage_logs"
            },
            "sample_data": [dict(s) for s in samples] if samples else []
        }
        
    except Exception as e:
        logger.error(f"Error fixing agent metrics v2: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fix agent metrics: {str(e)}"
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
        
        # Check current agent stats
        agent_stats_query = """
            SELECT 
                total_requests,
                request_count,
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

