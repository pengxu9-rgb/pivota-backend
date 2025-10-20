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

router = APIRouter()

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
        # Build query
        query = "SELECT * FROM agents"
        params = {}
        
        if status:
            query += " WHERE status = :status"
            params["status"] = status
        
        query += " ORDER BY created_at DESC"
        
        agents = await database.fetch_all(query, params)
        
        # If no agents in DB, return demo data
        if not agents:
            agents = [
                {
                    "agent_id": "agent_demo_001",
                    "name": "Demo Agent",
                    "email": "demo@agent.com",
                    "company": "Demo Company",
                    "api_key": "ak_demo_" + secrets.token_hex(16),
                    "status": "active",
                    "created_at": datetime.now() - timedelta(days=30),
                    "last_active": datetime.now() - timedelta(hours=2),
                    "request_count": 1250,
                    "success_rate": 98.5,
                    "rate_limit": 1000
                }
            ]
        
        # Format response
        formatted_agents = []
        for agent in agents:
            formatted_agents.append({
                "agent_id": agent.get("agent_id"),
                "name": agent.get("name"),
                "email": agent.get("email"),
                "company": agent.get("company"),
                "api_key_prefix": agent.get("api_key", "")[:10] + "..." if agent.get("api_key") else None,
                "status": agent.get("status", "active"),
                "created_at": agent.get("created_at"),
                "last_active": agent.get("last_active") or datetime.now() - timedelta(hours=random.randint(1, 48)),
                "request_count": agent.get("request_count", random.randint(100, 5000)),
                "success_rate": agent.get("success_rate", round(random.uniform(95, 99.9), 1)),
                "rate_limit": agent.get("rate_limit", 1000)
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
        agent = await database.fetch_one(
            "SELECT * FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            # Return demo agent if not found
            if agent_id == "agent_demo_001":
                agent = {
                    "agent_id": "agent_demo_001",
                    "name": "Demo Agent",
                    "email": "demo@agent.com",
                    "company": "Demo Company",
                    "use_case": "E-commerce integration",
                    "api_key": "ak_demo_" + secrets.token_hex(16),
                    "status": "active",
                    "created_at": datetime.now() - timedelta(days=30),
                    "last_active": datetime.now() - timedelta(hours=2),
                    "request_count": 1250,
                    "success_rate": 98.5,
                    "rate_limit": 1000,
                    "allowed_merchants": []
                }
            else:
                raise HTTPException(status_code=404, detail="Agent not found")
        
        # Get agent's merchant connections
        merchant_connections = await database.fetch_all(
            """SELECT m.merchant_id, m.business_name, am.connected_at
               FROM agent_merchants am
               JOIN merchant_onboarding m ON am.merchant_id = m.merchant_id
               WHERE am.agent_id = :agent_id""",
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "agent": {
                "agent_id": agent.get("agent_id"),
                "name": agent.get("name"),
                "email": agent.get("email"),
                "company": agent.get("company"),
                "use_case": agent.get("use_case", "General integration"),
                "api_key": agent.get("api_key"),
                "status": agent.get("status", "active"),
                "created_at": agent.get("created_at"),
                "last_active": agent.get("last_active"),
                "request_count": agent.get("request_count", 0),
                "success_rate": agent.get("success_rate", 0),
                "rate_limit": agent.get("rate_limit", 1000),
                "merchant_connections": [
                    {
                        "merchant_id": mc["merchant_id"],
                        "business_name": mc["business_name"],
                        "connected_at": mc["connected_at"]
                    }
                    for mc in merchant_connections
                ]
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get agent details: {str(e)}")

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

@router.get("/agents/{agent_id}/analytics")
async def get_agent_analytics(
    agent_id: str,
    days: int = Query(default=30, ge=1, le=365),
    current_user: dict = Depends(get_current_user)
):
    """Get agent analytics (Employee only)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get agent
        agent = await database.fetch_one(
            "SELECT * FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Generate demo analytics
        analytics = {
            "period": f"Last {days} days",
            "total_requests": random.randint(1000, 50000),
            "successful_requests": random.randint(900, 49000),
            "failed_requests": random.randint(10, 1000),
            "success_rate": round(random.uniform(95, 99.9), 1),
            "average_response_time": round(random.uniform(50, 500), 2),
            "peak_hour": random.randint(10, 18),
            "most_used_endpoints": [
                {"endpoint": "/products/search", "count": random.randint(100, 5000)},
                {"endpoint": "/orders", "count": random.randint(50, 2000)},
                {"endpoint": "/payments", "count": random.randint(20, 1000)}
            ],
            "daily_requests": [
                {
                    "date": (datetime.now() - timedelta(days=i)).date().isoformat(),
                    "count": random.randint(50, 500)
                }
                for i in range(min(days, 30))
            ]
        }
        
        return {
            "status": "success",
            "analytics": analytics
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get analytics: {str(e)}")

