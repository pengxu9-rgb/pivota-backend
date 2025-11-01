"""
Employee Agents Management API
Provides comprehensive agent monitoring and control for Employee Portal
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from db.database import database
from utils.auth import get_current_user
import secrets
import hashlib

router = APIRouter(prefix="/employee/agents", tags=["Employee Agents Management"])
logger = logging.getLogger(__name__)

# ============================================================================
# Response Models with Reserved Fields
# ============================================================================

class AgentMetrics(BaseModel):
    """预留的 metrics 字段结构"""
    requests_24h: int = 0
    successful_24h: int = 0
    failed_24h: int = 0
    success_rate: float = 0.0
    avg_latency_ms: int = 0
    total_gmv: float = 0.0
    total_orders: int = 0

class AgentGovernance(BaseModel):
    """预留的 governance 字段结构"""
    max_error_rate: float = 0.1
    max_requests_per_minute: int = 100
    policy_status: str = "active"
    last_violation: Optional[str] = None

class AgentResponse(BaseModel):
    """Agent 详细信息（包含预留字段）"""
    agent_id: str
    name: str
    email: str
    company: Optional[str]
    use_case: Optional[str]
    status: str
    api_key: Optional[str]
    rate_limit: int
    created_at: str
    last_active: Optional[str]
    
    # 预留字段（前端可直接使用）
    metrics: AgentMetrics
    governance: AgentGovernance
    merchant_count: int = 0

# ============================================================================
# Main Endpoints
# ============================================================================

@router.get("/")
async def get_all_agents(
    status_filter: Optional[str] = Query(None, description="Filter by status: active, inactive, suspended"),
    date_range: Optional[str] = Query("7d", description="Date range: 1d, 7d, 30d, 90d"),
    current_user: dict = Depends(get_current_user)
):
    """
    获取所有 agents 及其统计信息
    包含预留的 metrics 和 governance 字段
    支持时间范围过滤: 1d (今天), 7d (7天), 30d (30天), 90d (90天)
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build query with optional filter
        where_clause = ""
        params = {}
        if status_filter:
            where_clause = "WHERE a.status = :status"
            params["status"] = status_filter
        
        # Main query - get agents with REAL stats from orders table
        query = f"""
            SELECT 
                a.agent_id,
                a.name,
                a.email,
                a.company,
                a.use_case,
                a.status,
                a.api_key,
                a.rate_limit,
                a.created_at,
                a.last_active,
                COALESCE(o.total_orders, 0) as request_count,
                COALESCE(o.success_rate, 0) as success_rate,
                COALESCE(o.total_orders, 0) as total_orders,
                COALESCE(o.total_gmv, 0) as total_gmv,
                COALESCE(o.merchant_count, 0) as merchant_count
            FROM agents a
            LEFT JOIN (
                SELECT 
                    agent_id,
                    COUNT(*) as total_orders,
                    COALESCE(SUM(total), 0) as total_gmv,
                    COUNT(DISTINCT merchant_id) as merchant_count,
                    CASE 
                        WHEN COUNT(*) > 0 THEN 
                            (COUNT(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 END)::FLOAT / COUNT(*)::FLOAT * 100)
                        ELSE 0 
                    END as success_rate
                FROM orders
                WHERE agent_id IS NOT NULL
                GROUP BY agent_id
            ) o ON a.agent_id = o.agent_id
            {where_clause}
            GROUP BY a.agent_id, o.total_orders, o.success_rate, o.total_gmv, o.merchant_count
            ORDER BY a.created_at DESC
        """
        
        agents = await database.fetch_all(query, params)
        
        # Enrich with metrics and governance
        result = []
        for agent in agents:
            # Determine time interval based on date_range
            time_interval = {
                "1d": "24 hours",
                "7d": "7 days",
                "30d": "30 days",
                "90d": "90 days"
            }.get(date_range, "7 days")
            
            # Get metrics from ORDERS table (not usage_logs) - same as merchant!
            metrics_query = f"""
                SELECT 
                    COUNT(*) as requests_24h,
                    COUNT(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 END) as successful_24h,
                    COUNT(CASE WHEN payment_status IN ('failed', 'cancelled', 'error') THEN 1 END) as failed_24h,
                    CASE 
                        WHEN COUNT(*) > 0 THEN 
                            (COUNT(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 END)::FLOAT / COUNT(*)::FLOAT * 100)
                        ELSE 0 
                    END as success_rate_24h,
                    0 as avg_latency_24h,
                    COUNT(*) as orders_24h,
                    COALESCE(SUM(total), 0) as gmv_24h
                FROM orders
                WHERE agent_id = :agent_id
                    AND created_at >= NOW() - INTERVAL '{time_interval}'
            """
            
            metrics_row = await database.fetch_one(metrics_query, {"agent_id": agent["agent_id"]})
            
            # If no metrics, provide default values
            if not metrics_row:
                metrics_row = {
                    "requests_24h": 0,
                    "successful_24h": 0,
                    "failed_24h": 0,
                    "success_rate_24h": 0,
                    "avg_latency_24h": 0,
                    "orders_24h": 0,
                    "gmv_24h": 0
                }
            
            # Get governance policy
            policy_query = """
                SELECT max_error_rate, max_requests_per_minute, status
                FROM agent_policies
                WHERE agent_id = :agent_id
            """
            policy_row = await database.fetch_one(policy_query, {"agent_id": agent["agent_id"]})
            
            # Build response with REAL data from orders table
            result.append({
                "agent_id": agent["agent_id"],
                "name": agent["name"],
                "email": agent["email"],
                "company": agent["company"],
                "use_case": agent["use_case"],
                "status": agent["status"],
                "api_key": agent["api_key"],
                "rate_limit": agent["rate_limit"],
                "created_at": agent["created_at"].isoformat() if agent["created_at"] else None,
                "last_active": agent["last_active"].isoformat() if agent["last_active"] else None,
                "merchant_count": agent["merchant_count"],
                
                # Add total stats from orders table
                "total_orders": agent["total_orders"],
                "total_gmv": float(agent["total_gmv"]),
                "total_requests": agent["request_count"],  # For compatibility
                
                # Reserved: metrics field (always present)
                "metrics": {
                    "requests_24h": metrics_row["requests_24h"] if metrics_row else 0,
                    "successful_24h": metrics_row["successful_24h"] if metrics_row else 0,
                    "failed_24h": metrics_row["failed_24h"] if metrics_row else 0,
                    "success_rate": float(metrics_row.get("success_rate_24h", 0)) if metrics_row else 0.0,
                    "avg_latency_ms": int(metrics_row.get("avg_latency_24h", 0)) if metrics_row else 0,
                    "total_gmv": float(metrics_row.get("gmv_24h", 0)) if metrics_row else 0.0,
                    "total_orders": metrics_row.get("orders_24h", 0) if metrics_row else 0
                },
                
                # Reserved: governance field (always present)
                "governance": {
                    "max_error_rate": float(policy_row["max_error_rate"]) if policy_row else 0.1,
                    "max_requests_per_minute": policy_row["max_requests_per_minute"] if policy_row else 100,
                    "policy_status": policy_row["status"] if policy_row else "active",
                    "last_violation": None  # TODO: track violations
                }
            })
        
        return {
            "status": "success",
            "agents": result,
            "total": len(result)
        }
        
    except Exception as e:
        logger.error(f"Failed to get agents: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to fetch agents: {str(e)}")


@router.get("/{agent_id}/details")
async def get_agent_details(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    获取 agent 详细信息（含 metrics 和 governance）
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get agent基本信息
        agent_query = """
            SELECT * FROM agents WHERE agent_id = :agent_id
        """
        agent = await database.fetch_one(agent_query, {"agent_id": agent_id})
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Get metrics (same logic as list endpoint)
        metrics_query = """
            SELECT * FROM agent_metrics_24h WHERE agent_id = :agent_id
        """
        metrics = await database.fetch_one(metrics_query, {"agent_id": agent_id})
        
        # Get policy
        policy_query = """
            SELECT * FROM agent_policies WHERE agent_id = :agent_id
        """
        policy = await database.fetch_one(policy_query, {"agent_id": agent_id})
        
        # Get connected merchants
        merchants_query = """
            SELECT merchant_id FROM agent_merchants WHERE agent_id = :agent_id
        """
        merchants = await database.fetch_all(merchants_query, {"agent_id": agent_id})
        
        return {
            "status": "success",
            "agent": {
                **dict(agent),
                "created_at": agent["created_at"].isoformat() if agent["created_at"] else None,
                "last_active": agent["last_active"].isoformat() if agent["last_active"] else None,
                "metrics": dict(metrics) if metrics else {},
                "governance": dict(policy) if policy else {},
                "merchants": [m["merchant_id"] for m in merchants]
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get agent details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}/calls")
async def get_agent_calls(
    agent_id: str,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user)
):
    """
    获取 agent 的 API 调用日志
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        query = """
            SELECT 
                id,
                agent_id,
                endpoint,
                method,
                merchant_id,
                status_code,
                response_time_ms,
                error_message,
                order_id,
                order_amount,
                timestamp
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
            ORDER BY timestamp DESC
            LIMIT :limit OFFSET :offset
        """
        
        calls = await database.fetch_all(query, {
            "agent_id": agent_id,
            "limit": limit,
            "offset": offset
        })
        
        # Get total count
        count_query = """
            SELECT COUNT(*) as total
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
        """
        count = await database.fetch_one(count_query, {"agent_id": agent_id})
        
        return {
            "status": "success",
            "calls": [
                {
                    "id": c["id"],
                    "endpoint": c["endpoint"],
                    "method": c["method"],
                    "merchant_id": c["merchant_id"],
                    "status_code": c["status_code"],
                    "response_time_ms": c["response_time_ms"],
                    "error_message": c["error_message"],
                    "order_id": c["order_id"],
                    "order_amount": float(c["order_amount"]) if c["order_amount"] else None,
                    "timestamp": c["timestamp"].isoformat() if c["timestamp"] else None
                }
                for c in calls
            ],
            "total": count["total"] if count else 0,
            "limit": limit,
            "offset": offset
        }
        
    except Exception as e:
        logger.error(f"Failed to get agent calls: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/reset-api-key")
async def reset_agent_api_key(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    重置 Agent API Key
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Generate new API key
        new_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Update in database
        query = """
            UPDATE agents
            SET api_key = :api_key,
                last_key_rotation = NOW()
            WHERE agent_id = :agent_id
            RETURNING api_key
        """
        
        result = await database.fetch_one(query, {
            "api_key": new_key,
            "agent_id": agent_id
        })
        
        if not result:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        logger.info(f"API key reset for agent {agent_id} by {current_user.get('email')}")
        
        return {
            "status": "success",
            "message": "API key reset successfully",
            "agent_id": agent_id,
            "new_api_key": result["api_key"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reset API key: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/update-rate-limit")
async def update_agent_rate_limit(
    agent_id: str,
    new_limit: int = Query(..., ge=10, le=10000),
    current_user: dict = Depends(get_current_user)
):
    """
    更新 Agent Rate Limit
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Update agents table
        agents_query = """
            UPDATE agents
            SET rate_limit = :rate_limit
            WHERE agent_id = :agent_id
        """
        await database.execute(agents_query, {
            "rate_limit": new_limit,
            "agent_id": agent_id
        })
        
        # Also update agent_policies if exists
        policy_query = """
            UPDATE agent_policies
            SET max_requests_per_minute = :rate_limit,
                updated_at = NOW()
            WHERE agent_id = :agent_id
        """
        await database.execute(policy_query, {
            "rate_limit": new_limit,
            "agent_id": agent_id
        })
        
        logger.info(f"Rate limit updated to {new_limit} for agent {agent_id} by {current_user.get('email')}")
        
        return {
            "status": "success",
            "message": f"Rate limit updated to {new_limit} req/min",
            "agent_id": agent_id,
            "new_rate_limit": new_limit
        }
        
    except Exception as e:
        logger.error(f"Failed to update rate limit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/deactivate")
async def deactivate_agent(
    agent_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """
    停用 Agent
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        query = """
            UPDATE agents
            SET status = 'inactive',
                deactivated_at = NOW()
            WHERE agent_id = :agent_id
        """
        
        await database.execute(query, {"agent_id": agent_id})
        
        logger.info(f"Agent {agent_id} deactivated by {current_user.get('email')}. Reason: {reason}")
        
        return {
            "status": "success",
            "message": "Agent deactivated",
            "agent_id": agent_id
        }
        
    except Exception as e:
        logger.error(f"Failed to deactivate agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/reactivate")
async def reactivate_agent(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    重新激活 Agent
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        query = """
            UPDATE agents
            SET status = 'active',
                deactivated_at = NULL
            WHERE agent_id = :agent_id
        """
        
        await database.execute(query, {"agent_id": agent_id})
        
        logger.info(f"Agent {agent_id} reactivated by {current_user.get('email')}")
        
        return {
            "status": "success",
            "message": "Agent reactivated",
            "agent_id": agent_id
        }
        
    except Exception as e:
        logger.error(f"Failed to reactivate agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))

