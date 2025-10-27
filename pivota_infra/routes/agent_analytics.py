"""
Agent Analytics Endpoints
Provides analytics data for agent dashboard (agent-scoped)
"""
from fastapi import APIRouter, Header, HTTPException, Query
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from db.database import database
from utils.auth import decode_token

router = APIRouter(prefix="/agent/v1", tags=["agent-analytics"])


async def resolve_agent_id(
    authorization: Optional[str] = None,
    x_api_key: Optional[str] = None
) -> str:
    """Helper to resolve agent_id from JWT or API key"""
    agent_id = None
    
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = decode_token(authorization.split(" ")[1])
            agent_id = payload.get("agent_id")
        except:
            pass
    
    if not agent_id and x_api_key:
        agent_row = await database.fetch_one(
            "SELECT agent_id FROM agents WHERE api_key = :key LIMIT 1",
            {"key": x_api_key}
        )
        if agent_row:
            agent_id = agent_row["agent_id"]
    
    if not agent_id:
        raise HTTPException(status_code=401, detail="Missing or invalid agent credentials")
    
    return agent_id


@router.get("/analytics/funnel")
async def get_conversion_funnel(
    days: int = Query(7, ge=1, le=90),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
) -> Dict[str, Any]:
    """
    Get order conversion funnel for this agent
    """
    try:
        agent_id = await resolve_agent_id(authorization, x_api_key)
        since = datetime.now() - timedelta(days=days)
        
        # Orders initiated (all orders created by this agent)
        orders_initiated = await database.fetch_val(
            """SELECT COUNT(*) FROM orders 
               WHERE agent_id = :agent_id 
               AND created_at >= :since 
               AND (is_deleted IS NULL OR is_deleted = FALSE)""",
            {"agent_id": agent_id, "since": since}
        ) or 0
        
        # Payment attempted (orders with payment_intent_id or payment_status != 'unpaid')
        payment_attempted = await database.fetch_val(
            """SELECT COUNT(*) FROM orders 
               WHERE agent_id = :agent_id 
               AND created_at >= :since 
               AND (is_deleted IS NULL OR is_deleted = FALSE)
               AND (payment_intent_id IS NOT NULL OR payment_status != 'unpaid')""",
            {"agent_id": agent_id, "since": since}
        ) or 0
        
        # Orders completed (payment_status = 'paid')
        orders_completed = await database.fetch_val(
            """SELECT COUNT(*) FROM orders 
               WHERE agent_id = :agent_id 
               AND created_at >= :since 
               AND (is_deleted IS NULL OR is_deleted = FALSE)
               AND payment_status = 'paid'""",
            {"agent_id": agent_id, "since": since}
        ) or 0
        
        return {
            "status": "success",
            "orders_initiated": orders_initiated,
            "payment_attempted": payment_attempted,
            "orders_completed": orders_completed,
            "conversion_rate": (orders_completed / orders_initiated * 100) if orders_initiated > 0 else 0
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "orders_initiated": 0,
            "payment_attempted": 0,
            "orders_completed": 0
        }


@router.get("/analytics/queries")
async def get_query_analytics(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
) -> Dict[str, Any]:
    """
    Get query analytics (product searches, inventory checks, price queries)
    Based on agent_usage_logs for this agent
    """
    try:
        agent_id = await resolve_agent_id(authorization, x_api_key)
        last_24h = datetime.now() - timedelta(hours=24)
        last_48h = datetime.now() - timedelta(hours=48)
        
        # Product searches (last 24h)
        product_searches = await database.fetch_val(
            """SELECT COUNT(*) FROM agent_usage_logs 
               WHERE agent_id = :agent_id 
               AND timestamp >= :since 
               AND endpoint LIKE '%/products%'""",
            {"agent_id": agent_id, "since": last_24h}
        ) or 0
        
        # Previous 24h for trend
        product_searches_prev = await database.fetch_val(
            """SELECT COUNT(*) FROM agent_usage_logs 
               WHERE agent_id = :agent_id 
               AND timestamp >= :since2 AND timestamp < :since1
               AND endpoint LIKE '%/products%'""",
            {"agent_id": agent_id, "since1": last_24h, "since2": last_48h}
        ) or 0
        
        # Inventory checks
        inventory_checks = await database.fetch_val(
            """SELECT COUNT(*) FROM agent_usage_logs 
               WHERE agent_id = :agent_id 
               AND timestamp >= :since 
               AND endpoint LIKE '%/inventory%'""",
            {"agent_id": agent_id, "since": last_24h}
        ) or 0
        
        # Price queries
        price_queries = await database.fetch_val(
            """SELECT COUNT(*) FROM agent_usage_logs 
               WHERE agent_id = :agent_id 
               AND timestamp >= :since 
               AND endpoint LIKE '%/pricing%'""",
            {"agent_id": agent_id, "since": last_24h}
        ) or 0
        
        # Calculate trends
        def get_trend(current, previous):
            if previous == 0:
                return "stable", 0
            change = ((current - previous) / previous * 100)
            if change > 5:
                return "up", round(change, 1)
            elif change < -5:
                return "down", round(abs(change), 1)
            else:
                return "stable", 0
        
        ps_trend, ps_change = get_trend(product_searches, product_searches_prev)
        
        return {
            "status": "success",
            "product_searches": product_searches,
            "product_searches_trend": ps_trend,
            "product_searches_change": ps_change,
            "inventory_checks": inventory_checks,
            "inventory_checks_trend": "stable",
            "price_queries": price_queries,
            "price_queries_trend": "stable"
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "product_searches": 0,
            "inventory_checks": 0,
            "price_queries": 0
        }
