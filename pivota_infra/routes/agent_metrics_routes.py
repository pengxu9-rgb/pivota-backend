"""
Agent Metrics Routes
API endpoints for tracking agent performance and API usage
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from db.database import database
from db.agents import agents  # Use agents table instead
from utils.logger import logger

router = APIRouter(prefix="/api/agent-metrics", tags=["agent-metrics"])

class AgentMetrics(BaseModel):
    agent_id: str
    total_api_calls: int
    active_connections: int
    product_searches: int
    inventory_checks: int
    price_queries: int
    orders_initiated: int
    orders_completed: int
    total_gmv: float
    avg_order_value: float
    conversion_rate: float
    avg_response_time: int  # ms

class APIUsageLog(BaseModel):
    agent_id: str
    endpoint: str
    method: str
    response_time: int  # ms
    status_code: int
    timestamp: datetime

# In-memory storage for demo (should use database in production)
api_usage_logs: List[APIUsageLog] = []
agent_connections: Dict[str, datetime] = {}

@router.get("/dashboard/{agent_id}")
async def get_agent_dashboard(agent_id: str):
    """
    Get dashboard metrics for a specific agent
    """
    try:
        # Verify agent exists
        query = agents.select().where(agents.c.agent_id == agent_id)
        agent = await database.fetch_one(query)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Calculate metrics from logs (in production, aggregate from database)
        agent_logs = [log for log in api_usage_logs if log.agent_id == agent_id]
        
        # Count different query types
        product_searches = len([l for l in agent_logs if 'product' in l.endpoint.lower() or 'search' in l.endpoint.lower()])
        inventory_checks = len([l for l in agent_logs if 'inventory' in l.endpoint.lower()])
        price_queries = len([l for l in agent_logs if 'price' in l.endpoint.lower() or 'pricing' in l.endpoint.lower()])
        
        # Calculate order metrics (would query orders table in production)
        orders_initiated = 89  # Mock data
        orders_completed = 76  # Mock data
        conversion_rate = (orders_completed / orders_initiated * 100) if orders_initiated > 0 else 0
        
        # Calculate response times
        response_times = [log.response_time for log in agent_logs]
        avg_response_time = sum(response_times) / len(response_times) if response_times else 145
        
        return {
            "agent_id": agent_id,
            "agent_name": agent.get("name", "Unknown Agent"),
            "metrics": {
                "total_api_calls": len(agent_logs),
                "active_connections": 1 if agent_id in agent_connections else 0,
                "product_searches": product_searches,
                "inventory_checks": inventory_checks,
                "price_queries": price_queries,
                "orders_initiated": orders_initiated,
                "orders_completed": orders_completed,
                "conversion_rate": round(conversion_rate, 1),
                "total_gmv": 45280.50,  # Mock - would calculate from orders
                "avg_order_value": 596.32,  # Mock
                "avg_response_time": int(avg_response_time),
                "payment_success_rate": 94.2  # Mock
            },
            "status": "active" if agent_id in agent_connections else "inactive",
            "last_activity": agent_connections.get(agent_id, datetime.now()).isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get dashboard metrics")

@router.get("/summary")
async def get_agents_summary():
    """
    Get summary metrics for all agents (platform overview)
    """
    try:
        # Get all active agents
        active_agents = len(agent_connections)
        total_agents = 47  # Mock - would query database
        
        # Calculate total API calls
        total_calls = len(api_usage_logs)
        
        # Calculate query breakdown
        product_searches = len([l for l in api_usage_logs if 'product' in l.endpoint.lower() or 'search' in l.endpoint.lower()])
        inventory_checks = len([l for l in api_usage_logs if 'inventory' in l.endpoint.lower()])
        price_queries = len([l for l in api_usage_logs if 'price' in l.endpoint.lower() or 'pricing' in l.endpoint.lower()])
        
        # Mock top agents (in production, aggregate from database)
        top_agents = [
            {"name": "ShopifyBot", "calls": 1234, "orders": 34, "gmv": 12450, "conversion": 2.8},
            {"name": "CustomerAI", "calls": 890, "orders": 28, "gmv": 8920, "conversion": 3.1},
            {"name": "SalesAssist", "calls": 678, "orders": 19, "gmv": 6780, "conversion": 2.8}
        ]
        
        return {
            "overview": {
                "total_integrations": total_agents,
                "active_connections": active_agents,
                "api_calls_today": total_calls if total_calls > 0 else 3429,  # Mock fallback
                "avg_response_time": 145  # ms
            },
            "mcp_queries": {
                "product_searches": product_searches if product_searches > 0 else 892,  # Mock fallback
                "inventory_checks": inventory_checks if inventory_checks > 0 else 456,
                "price_queries": price_queries if price_queries > 0 else 234
            },
            "order_funnel": {
                "orders_initiated": 89,
                "orders_completed": 76,
                "conversion_rate": 85.4,
                "total_gmv": 45280.50,
                "avg_order_value": 596.32,
                "payment_success_rate": 94.2
            },
            "top_agents": top_agents,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting agents summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get summary metrics")

@router.post("/log")
async def log_api_usage(
    agent_id: str,
    endpoint: str,
    method: str = "GET",
    response_time: int = 100,
    status_code: int = 200
):
    """
    Log API usage for an agent
    """
    try:
        log_entry = APIUsageLog(
            agent_id=agent_id,
            endpoint=endpoint,
            method=method,
            response_time=response_time,
            status_code=status_code,
            timestamp=datetime.now()
        )
        
        api_usage_logs.append(log_entry)
        
        # Update agent connection status
        agent_connections[agent_id] = datetime.now()
        
        # Clean up old logs (keep last 1000)
        if len(api_usage_logs) > 1000:
            api_usage_logs[:] = api_usage_logs[-1000:]
        
        return {
            "status": "success",
            "message": "Usage logged",
            "timestamp": log_entry.timestamp.isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error logging API usage: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to log usage")

@router.get("/leaderboard")
async def get_agent_leaderboard(
    metric: str = Query("orders", description="Metric to rank by: orders, gmv, conversion"),
    limit: int = Query(10, description="Number of agents to return")
):
    """
    Get agent leaderboard by various metrics
    """
    try:
        # Mock leaderboard data (in production, query and aggregate from database)
        agents = [
            {"id": "agent_001", "name": "ShopifyBot", "calls": 1234, "orders": 34, "gmv": 12450, "conversion": 2.8},
            {"id": "agent_002", "name": "CustomerAI", "calls": 890, "orders": 28, "gmv": 8920, "conversion": 3.1},
            {"id": "agent_003", "name": "SalesAssist", "calls": 678, "orders": 19, "gmv": 6780, "conversion": 2.8},
            {"id": "agent_004", "name": "ChatCommerce", "calls": 456, "orders": 15, "gmv": 4560, "conversion": 3.3},
            {"id": "agent_005", "name": "BuyBot", "calls": 234, "orders": 12, "gmv": 3420, "conversion": 5.1},
        ]
        
        # Sort by requested metric
        if metric == "gmv":
            agents.sort(key=lambda x: x["gmv"], reverse=True)
        elif metric == "conversion":
            agents.sort(key=lambda x: x["conversion"], reverse=True)
        else:  # default to orders
            agents.sort(key=lambda x: x["orders"], reverse=True)
        
        return {
            "leaderboard": agents[:limit],
            "metric": metric,
            "period": "last_30_days",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting leaderboard: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get leaderboard")

@router.get("/rate-limits/{agent_id}")
async def get_rate_limits(agent_id: str):
    """
    Get rate limit status for an agent
    """
    try:
        # Verify agent exists
        query = agents.select().where(agents.c.agent_id == agent_id)
        agent = await database.fetch_one(query)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Mock rate limit data (in production, track actual usage)
        return {
            "agent_id": agent_id,
            "limits": {
                "requests_per_minute": {"used": 234, "limit": 1000, "remaining": 766},
                "requests_per_hour": {"used": 8432, "limit": 50000, "remaining": 41568},
                "requests_per_day": {"used": 45678, "limit": 1000000, "remaining": 954322}
            },
            "reset_times": {
                "minute": (datetime.now() + timedelta(seconds=60 - datetime.now().second)).isoformat(),
                "hour": (datetime.now() + timedelta(minutes=60 - datetime.now().minute)).isoformat(),
                "day": (datetime.now() + timedelta(hours=24 - datetime.now().hour)).isoformat()
            },
            "status": "healthy",
            "warnings": []
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting rate limits: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get rate limits")






from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from db.database import database
from db.agents import agents  # Use agents table instead
from utils.logger import logger

router = APIRouter(prefix="/api/agent-metrics", tags=["agent-metrics"])

class AgentMetrics(BaseModel):
    agent_id: str
    total_api_calls: int
    active_connections: int
    product_searches: int
    inventory_checks: int
    price_queries: int
    orders_initiated: int
    orders_completed: int
    total_gmv: float
    avg_order_value: float
    conversion_rate: float
    avg_response_time: int  # ms

class APIUsageLog(BaseModel):
    agent_id: str
    endpoint: str
    method: str
    response_time: int  # ms
    status_code: int
    timestamp: datetime

# In-memory storage for demo (should use database in production)
api_usage_logs: List[APIUsageLog] = []
agent_connections: Dict[str, datetime] = {}

@router.get("/dashboard/{agent_id}")
async def get_agent_dashboard(agent_id: str):
    """
    Get dashboard metrics for a specific agent
    """
    try:
        # Verify agent exists
        query = agents.select().where(agents.c.agent_id == agent_id)
        agent = await database.fetch_one(query)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Calculate metrics from logs (in production, aggregate from database)
        agent_logs = [log for log in api_usage_logs if log.agent_id == agent_id]
        
        # Count different query types
        product_searches = len([l for l in agent_logs if 'product' in l.endpoint.lower() or 'search' in l.endpoint.lower()])
        inventory_checks = len([l for l in agent_logs if 'inventory' in l.endpoint.lower()])
        price_queries = len([l for l in agent_logs if 'price' in l.endpoint.lower() or 'pricing' in l.endpoint.lower()])
        
        # Calculate order metrics (would query orders table in production)
        orders_initiated = 89  # Mock data
        orders_completed = 76  # Mock data
        conversion_rate = (orders_completed / orders_initiated * 100) if orders_initiated > 0 else 0
        
        # Calculate response times
        response_times = [log.response_time for log in agent_logs]
        avg_response_time = sum(response_times) / len(response_times) if response_times else 145
        
        return {
            "agent_id": agent_id,
            "agent_name": agent.get("name", "Unknown Agent"),
            "metrics": {
                "total_api_calls": len(agent_logs),
                "active_connections": 1 if agent_id in agent_connections else 0,
                "product_searches": product_searches,
                "inventory_checks": inventory_checks,
                "price_queries": price_queries,
                "orders_initiated": orders_initiated,
                "orders_completed": orders_completed,
                "conversion_rate": round(conversion_rate, 1),
                "total_gmv": 45280.50,  # Mock - would calculate from orders
                "avg_order_value": 596.32,  # Mock
                "avg_response_time": int(avg_response_time),
                "payment_success_rate": 94.2  # Mock
            },
            "status": "active" if agent_id in agent_connections else "inactive",
            "last_activity": agent_connections.get(agent_id, datetime.now()).isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get dashboard metrics")

@router.get("/summary")
async def get_agents_summary():
    """
    Get summary metrics for all agents (platform overview)
    """
    try:
        # Get all active agents
        active_agents = len(agent_connections)
        total_agents = 47  # Mock - would query database
        
        # Calculate total API calls
        total_calls = len(api_usage_logs)
        
        # Calculate query breakdown
        product_searches = len([l for l in api_usage_logs if 'product' in l.endpoint.lower() or 'search' in l.endpoint.lower()])
        inventory_checks = len([l for l in api_usage_logs if 'inventory' in l.endpoint.lower()])
        price_queries = len([l for l in api_usage_logs if 'price' in l.endpoint.lower() or 'pricing' in l.endpoint.lower()])
        
        # Mock top agents (in production, aggregate from database)
        top_agents = [
            {"name": "ShopifyBot", "calls": 1234, "orders": 34, "gmv": 12450, "conversion": 2.8},
            {"name": "CustomerAI", "calls": 890, "orders": 28, "gmv": 8920, "conversion": 3.1},
            {"name": "SalesAssist", "calls": 678, "orders": 19, "gmv": 6780, "conversion": 2.8}
        ]
        
        return {
            "overview": {
                "total_integrations": total_agents,
                "active_connections": active_agents,
                "api_calls_today": total_calls if total_calls > 0 else 3429,  # Mock fallback
                "avg_response_time": 145  # ms
            },
            "mcp_queries": {
                "product_searches": product_searches if product_searches > 0 else 892,  # Mock fallback
                "inventory_checks": inventory_checks if inventory_checks > 0 else 456,
                "price_queries": price_queries if price_queries > 0 else 234
            },
            "order_funnel": {
                "orders_initiated": 89,
                "orders_completed": 76,
                "conversion_rate": 85.4,
                "total_gmv": 45280.50,
                "avg_order_value": 596.32,
                "payment_success_rate": 94.2
            },
            "top_agents": top_agents,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting agents summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get summary metrics")

@router.post("/log")
async def log_api_usage(
    agent_id: str,
    endpoint: str,
    method: str = "GET",
    response_time: int = 100,
    status_code: int = 200
):
    """
    Log API usage for an agent
    """
    try:
        log_entry = APIUsageLog(
            agent_id=agent_id,
            endpoint=endpoint,
            method=method,
            response_time=response_time,
            status_code=status_code,
            timestamp=datetime.now()
        )
        
        api_usage_logs.append(log_entry)
        
        # Update agent connection status
        agent_connections[agent_id] = datetime.now()
        
        # Clean up old logs (keep last 1000)
        if len(api_usage_logs) > 1000:
            api_usage_logs[:] = api_usage_logs[-1000:]
        
        return {
            "status": "success",
            "message": "Usage logged",
            "timestamp": log_entry.timestamp.isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error logging API usage: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to log usage")

@router.get("/leaderboard")
async def get_agent_leaderboard(
    metric: str = Query("orders", description="Metric to rank by: orders, gmv, conversion"),
    limit: int = Query(10, description="Number of agents to return")
):
    """
    Get agent leaderboard by various metrics
    """
    try:
        # Mock leaderboard data (in production, query and aggregate from database)
        agents = [
            {"id": "agent_001", "name": "ShopifyBot", "calls": 1234, "orders": 34, "gmv": 12450, "conversion": 2.8},
            {"id": "agent_002", "name": "CustomerAI", "calls": 890, "orders": 28, "gmv": 8920, "conversion": 3.1},
            {"id": "agent_003", "name": "SalesAssist", "calls": 678, "orders": 19, "gmv": 6780, "conversion": 2.8},
            {"id": "agent_004", "name": "ChatCommerce", "calls": 456, "orders": 15, "gmv": 4560, "conversion": 3.3},
            {"id": "agent_005", "name": "BuyBot", "calls": 234, "orders": 12, "gmv": 3420, "conversion": 5.1},
        ]
        
        # Sort by requested metric
        if metric == "gmv":
            agents.sort(key=lambda x: x["gmv"], reverse=True)
        elif metric == "conversion":
            agents.sort(key=lambda x: x["conversion"], reverse=True)
        else:  # default to orders
            agents.sort(key=lambda x: x["orders"], reverse=True)
        
        return {
            "leaderboard": agents[:limit],
            "metric": metric,
            "period": "last_30_days",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting leaderboard: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get leaderboard")

@router.get("/rate-limits/{agent_id}")
async def get_rate_limits(agent_id: str):
    """
    Get rate limit status for an agent
    """
    try:
        # Verify agent exists
        query = agents.select().where(agents.c.agent_id == agent_id)
        agent = await database.fetch_one(query)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Mock rate limit data (in production, track actual usage)
        return {
            "agent_id": agent_id,
            "limits": {
                "requests_per_minute": {"used": 234, "limit": 1000, "remaining": 766},
                "requests_per_hour": {"used": 8432, "limit": 50000, "remaining": 41568},
                "requests_per_day": {"used": 45678, "limit": 1000000, "remaining": 954322}
            },
            "reset_times": {
                "minute": (datetime.now() + timedelta(seconds=60 - datetime.now().second)).isoformat(),
                "hour": (datetime.now() + timedelta(minutes=60 - datetime.now().minute)).isoformat(),
                "day": (datetime.now() + timedelta(hours=24 - datetime.now().hour)).isoformat()
            },
            "status": "healthy",
            "warnings": []
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting rate limits: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get rate limits")










