"""
Employee Portal Agent Management
Handles agent CRUD operations for employees
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
import uuid
import secrets
import random

router = APIRouter(prefix="/employee", tags=["employee-agents"])

# ============== Models ==============

class CreateAgentRequest(BaseModel):
    name: str
    email: EmailStr
    company: str
    use_case: str
    expected_volume: Optional[int] = 100

class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    company: Optional[str] = None
    status: Optional[str] = None
    rate_limit: Optional[int] = None

# ============== Agent Management ==============

@router.get("/agents")
async def get_all_agents(
    status: Optional[str] = Query(None, description="Filter by status: active, inactive, suspended"),
    current_user: dict = Depends(get_current_user)
):
    """Get all agents (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Query agents with merchant count from orders table
        where_condition = ""
        params = {}
        
        if status:
            where_condition = "WHERE a.status = :status"
            params["status"] = status
        
        query = f"""
            SELECT 
                a.*,
                COUNT(DISTINCT o.merchant_id) as merchant_count
            FROM agents a
            LEFT JOIN orders o ON a.agent_id = o.agent_id AND o.merchant_id IS NOT NULL
            {where_condition}
            GROUP BY a.agent_id
            ORDER BY a.created_at DESC
        """
        
        agents = await database.fetch_all(query, params)
        
        # Format response - match frontend expectations
        formatted_agents = []
        for agent in agents:
            # Use direct dict() conversion - works with databases.Record
            agent_dict = dict(agent)
            
            # Safe access with defaults
            api_key = agent_dict.get("api_key") or ""
            api_key_prefix = api_key[:10] + "..." if len(api_key) > 10 else None
            
            formatted_agents.append({
                "agent_id": agent_dict.get("agent_id"),
                "agent_name": agent_dict.get("name") or "Unknown Agent",
                "owner_email": agent_dict.get("email"),
                "agent_type": agent_dict.get("agent_type") or "Generic",
                "company": agent_dict.get("company"),
                "api_key_prefix": api_key_prefix,
                "status": agent_dict.get("status") or "active",
                "is_active": (agent_dict.get("status") or "active") == "active",
                "created_at": str(agent_dict.get("created_at")) if agent_dict.get("created_at") else None,
                "last_active": str(agent_dict.get("last_active")) if agent_dict.get("last_active") else None,
                "request_count": agent_dict.get("request_count") or 0,
                "success_rate": agent_dict.get("success_rate") or 0,
                "rate_limit": agent_dict.get("rate_limit") or 1000,
                "total_orders": agent_dict.get("total_orders") or 0,
                "total_gmv": float(agent_dict.get("total_gmv") or 0),
                "total_requests": agent_dict.get("total_requests") or agent_dict.get("request_count") or 0,
                "merchant_count": agent_dict.get("merchant_count") or 0
            })
        
        return {
            "status": "success",
            "agents": formatted_agents,
            "total": len(formatted_agents)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get agents: {str(e)}")

@router.get("/agents/{agent_id}")
async def get_agent_details(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get detailed agent information (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        agent_row = await database.fetch_one(
            "SELECT * FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent_row:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Convert to dict
        agent = dict(agent_row)
        
        # Get agent's merchant connections
        merchant_connections = await database.fetch_all(
            """SELECT m.merchant_id, m.business_name, am.connected_at
               FROM agent_merchants am
               JOIN merchant_onboarding m ON am.merchant_id = m.merchant_id
               WHERE am.agent_id = :agent_id""",
            {"agent_id": agent_id}
        )
        
        # Calculate merchant count from orders
        merchant_count_result = await database.fetch_one(
            "SELECT COUNT(DISTINCT merchant_id) as count FROM orders WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        merchant_count = dict(merchant_count_result).get("count", 0) if merchant_count_result else 0
        
        return {
            "status": "success",
            "agent": {
                "agent_id": agent.get("agent_id"),
                "name": agent.get("name"),
                "email": agent.get("email"),
                "company": agent.get("company"),
                "use_case": agent.get("use_case") or "General integration",
                "api_key": agent.get("api_key"),
                "status": agent.get("status") or "active",
                "created_at": agent.get("created_at"),
                "last_active": agent.get("last_active"),
                "request_count": agent.get("request_count") or 0,
                "success_rate": agent.get("success_rate") or 0,
                "rate_limit": agent.get("rate_limit") or 1000,
                "total_orders": agent.get("total_orders") or 0,
                "total_gmv": float(agent.get("total_gmv") or 0),
                "total_requests": agent.get("total_requests") or agent.get("request_count") or 0,
                "merchant_count": merchant_count,
                "merchants": [dict(mc) for mc in merchant_connections],
                "merchant_connections": [
                    {
                        "merchant_id": dict(mc).get("merchant_id"),
                        "business_name": dict(mc).get("business_name"),
                        "connected_at": dict(mc).get("connected_at")
                    }
                    for mc in merchant_connections
                ]
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get agent details: {str(e)}")

@router.get("/agents/{agent_id}/calls")
async def get_agent_calls(
    agent_id: str,
    limit: int = 100,
    offset: int = 0,
    current_user: dict = Depends(get_current_user)
):
    """Get agent API call logs"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get calls from agent_usage_logs
        calls = await database.fetch_all(
            """SELECT * FROM agent_usage_logs 
               WHERE agent_id = :agent_id 
               ORDER BY timestamp DESC 
               LIMIT :limit OFFSET :offset""",
            {"agent_id": agent_id, "limit": limit, "offset": offset}
        )
        
        # Count total
        total_result = await database.fetch_one(
            "SELECT COUNT(*) as total FROM agent_usage_logs WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        total = dict(total_result).get("total", 0) if total_result else 0
        
        # Format calls
        formatted_calls = []
        for call_row in calls:
            call = dict(call_row)
            formatted_calls.append({
                "id": call.get("id"),
                "endpoint": call.get("endpoint"),
                "method": call.get("method"),
                "merchant_id": call.get("merchant_id"),
                "status_code": call.get("status_code"),
                "response_time_ms": call.get("response_time_ms"),
                "error_message": call.get("error_message"),
                "order_id": call.get("order_id"),
                "order_amount": call.get("order_amount"),
                "timestamp": str(call.get("timestamp")) if call.get("timestamp") else None
            })
        
        return {
            "status": "success",
            "calls": formatted_calls,
            "total": total,
            "limit": limit,
            "offset": offset
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get calls: {str(e)}")

@router.post("/agents/create")
async def create_agent(
    request: CreateAgentRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create a new agent (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if email already exists
        existing = await database.fetch_one(
            "SELECT agent_id FROM agents WHERE email = :email",
            {"email": request.email}
        )
        
        if existing:
            raise HTTPException(status_code=400, detail="Agent with this email already exists")
        
        # Generate agent credentials
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        api_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Create agent
        await database.execute(
            """INSERT INTO agents 
               (agent_id, name, email, company, use_case, api_key, status, 
                created_at, rate_limit, request_count, success_rate)
               VALUES (:agent_id, :name, :email, :company, :use_case, :api_key, 
                       :status, :created_at, :rate_limit, :request_count, :success_rate)""",
            {
                "agent_id": agent_id,
                "name": request.name,
                "email": request.email,
                "company": request.company,
                "use_case": request.use_case,
                "api_key": api_key,
                "status": "active",
                "created_at": datetime.now(),
                "rate_limit": min(request.expected_volume * 10, 10000),
                "request_count": 0,
                "success_rate": 0
            }
        )
        
        return {
            "status": "success",
            "message": "Agent created successfully",
            "agent": {
                "agent_id": agent_id,
                "name": request.name,
                "email": request.email,
                "company": request.company,
                "api_key": api_key,
                "status": "active"
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}")

@router.post("/agents/{agent_id}/reset-api-key")
async def reset_agent_api_key(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Reset agent's API key (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if agent exists
        agent = await database.fetch_one(
            "SELECT agent_id FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Generate new API key
        new_api_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Update agent
        await database.execute(
            """UPDATE agents 
               SET api_key = :api_key, last_key_rotation = :rotation_time
               WHERE agent_id = :agent_id""",
            {
                "api_key": new_api_key,
                "rotation_time": datetime.now(),
                "agent_id": agent_id
            }
        )
        
        return {
            "status": "success",
            "message": "API key reset successfully",
            "new_api_key": new_api_key
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset API key: {str(e)}")

@router.post("/agents/{agent_id}/deactivate")
async def deactivate_agent(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Deactivate an agent (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if agent exists
        agent = await database.fetch_one(
            "SELECT agent_id, status FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        if agent["status"] == "inactive":
            raise HTTPException(status_code=400, detail="Agent is already inactive")
        
        # Deactivate agent
        await database.execute(
            """UPDATE agents 
               SET status = 'inactive', deactivated_at = :deactivated_at
               WHERE agent_id = :agent_id""",
            {
                "deactivated_at": datetime.now(),
                "agent_id": agent_id
            }
        )
        
        return {
            "status": "success",
            "message": "Agent deactivated successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to deactivate agent: {str(e)}")

@router.post("/agents/{agent_id}/activate")
async def activate_agent(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Activate an agent (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if agent exists
        agent = await database.fetch_one(
            "SELECT agent_id, status FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        if agent["status"] == "active":
            raise HTTPException(status_code=400, detail="Agent is already active")
        
        # Activate agent
        await database.execute(
            """UPDATE agents 
               SET status = 'active', deactivated_at = NULL
               WHERE agent_id = :agent_id""",
            {
                "agent_id": agent_id
            }
        )
        
        return {
            "status": "success",
            "message": "Agent activated successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to activate agent: {str(e)}")

# REMOVED: Analytics endpoint with random demo data
# This was generating fake data and should not be used






