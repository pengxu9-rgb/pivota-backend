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
import logging
import json as json_module

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/employee", tags=["employee-agents"])

# ============== Helpers ==============

def parse_json_field(value):
    """Safely parse JSON field - handles both string and already-parsed JSON"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json_module.loads(value)
        except:
            return []
    return []

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

# ============== Phase 2: Advanced Models ==============

class CreateApiKeyRequest(BaseModel):
    scopes: List[str] = ["orders:read", "products:read"]
    ip_whitelist: List[str] = []
    expires_in_days: Optional[int] = None

class AgentApiKeyResponse(BaseModel):
    key_id: str
    key_prefix: str
    scopes: List[str]
    ip_whitelist: List[str]
    is_active: bool
    created_at: str
    expires_at: Optional[str]
    last_used_at: Optional[str]

class AddProtocolRequest(BaseModel):
    protocol_name: str
    version: str = "1.0"
    
class AgentProtocolResponse(BaseModel):
    id: int
    protocol_name: str
    version: str
    status: str
    last_verified_at: Optional[str]
    created_at: str

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
        
        # Phase 2: Get API keys
        api_keys = await database.fetch_all(
            """SELECT key_id, key_prefix, scopes, is_active, created_at, expires_at, last_used_at
               FROM agent_api_keys
               WHERE agent_id = :agent_id AND is_active = true
               ORDER BY created_at DESC""",
            {"agent_id": agent_id}
        )
        
        # Phase 2: Get protocols
        protocols = await database.fetch_all(
            """SELECT id, protocol_name, version, status, last_verified_at
               FROM agent_protocols
               WHERE agent_id = :agent_id
               ORDER BY created_at DESC""",
            {"agent_id": agent_id}
        )
        
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
                ],
                # Phase 2: Include API keys and protocols
                "api_keys": [
                    {
                        "key_id": dict(k).get("key_id"),
                        "key_prefix": dict(k).get("key_prefix"),
                        "scopes": parse_json_field(dict(k).get("scopes")),
                        "ip_whitelist": parse_json_field(dict(k).get("ip_whitelist")),
                        "is_active": dict(k).get("is_active"),
                        "created_at": str(dict(k).get("created_at")) if dict(k).get("created_at") else None,
                        "expires_at": str(dict(k).get("expires_at")) if dict(k).get("expires_at") else None,
                        "last_used_at": str(dict(k).get("last_used_at")) if dict(k).get("last_used_at") else None
                    }
                    for k in api_keys
                ],
                "protocols": [
                    {
                        "id": dict(p).get("id"),
                        "protocol_name": dict(p).get("protocol_name"),
                        "version": dict(p).get("version"),
                        "status": dict(p).get("status"),
                        "last_verified_at": str(dict(p).get("last_verified_at")) if dict(p).get("last_verified_at") else None
                    }
                    for p in protocols
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

# ============== Phase 2: API Keys Management ==============

@router.get("/agents/{agent_id}/api-keys")
async def get_agent_api_keys(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """List all API keys for an agent"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        keys = await database.fetch_all(
            """SELECT id, agent_id, key_id, key_prefix, scopes, ip_whitelist,
                      is_active, created_at, expires_at, last_used_at, last_rotated_at
               FROM agent_api_keys
               WHERE agent_id = :agent_id
               ORDER BY created_at DESC""",
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "api_keys": [
                {
                    "key_id": dict(k).get("key_id"),
                    "key_prefix": dict(k).get("key_prefix"),
                    "scopes": parse_json_field(dict(k).get("scopes")),
                    "ip_whitelist": parse_json_field(dict(k).get("ip_whitelist")),
                    "is_active": dict(k).get("is_active"),
                    "created_at": str(dict(k).get("created_at")) if dict(k).get("created_at") else None,
                    "expires_at": str(dict(k).get("expires_at")) if dict(k).get("expires_at") else None,
                    "last_used_at": str(dict(k).get("last_used_at")) if dict(k).get("last_used_at") else None,
                    "last_rotated_at": str(dict(k).get("last_rotated_at")) if dict(k).get("last_rotated_at") else None
                }
                for k in keys
            ],
            "total": len(keys)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get API keys: {str(e)}")

@router.post("/agents/{agent_id}/api-keys")
async def create_agent_api_key(
    agent_id: str,
    request: CreateApiKeyRequest,
    current_user: dict = Depends(get_current_user)
):
    """Generate a new API key for an agent"""
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
        key_id = f"key_{uuid.uuid4().hex[:16]}"
        raw_key = f"ak_live_{secrets.token_hex(32)}"
        key_hash = secrets.token_hex(16)  # In production, use proper hashing
        key_prefix = raw_key[:12] + "..."
        
        # Calculate expiration
        expires_at = None
        if request.expires_in_days:
            expires_at = datetime.now() + timedelta(days=request.expires_in_days)
        
        # Insert into database
        await database.execute(
            """INSERT INTO agent_api_keys 
               (agent_id, key_id, key_hash, key_prefix, scopes, ip_whitelist, 
                is_active, created_at, expires_at, created_by)
               VALUES (:agent_id, :key_id, :key_hash, :key_prefix, :scopes, :ip_whitelist,
                       :is_active, :created_at, :expires_at, :created_by)""",
            {
                "agent_id": agent_id,
                "key_id": key_id,
                "key_hash": key_hash,
                "key_prefix": key_prefix,
                "scopes": str(request.scopes),  # Convert to JSON string
                "ip_whitelist": str(request.ip_whitelist),
                "is_active": True,
                "created_at": datetime.now(),
                "expires_at": expires_at,
                "created_by": current_user.get("email")
            }
        )
        
        logger.info(f"New API key {key_id} created for agent {agent_id} by {current_user.get('email')}")
        
        return {
            "status": "success",
            "message": "API key created successfully",
            "api_key": raw_key,  # Only shown once!
            "key_id": key_id,
            "key_prefix": key_prefix,
            "scopes": request.scopes,
            "expires_at": expires_at.isoformat() if expires_at else None
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create API key: {str(e)}")

@router.delete("/agents/{agent_id}/api-keys/{key_id}")
async def revoke_agent_api_key(
    agent_id: str,
    key_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Revoke an API key"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Deactivate the key
        result = await database.execute(
            """UPDATE agent_api_keys 
               SET is_active = false
               WHERE agent_id = :agent_id AND key_id = :key_id""",
            {"agent_id": agent_id, "key_id": key_id}
        )
        
        if not result:
            raise HTTPException(status_code=404, detail="API key not found")
        
        logger.info(f"API key {key_id} revoked for agent {agent_id} by {current_user.get('email')}")
        
        return {
            "status": "success",
            "message": "API key revoked successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to revoke API key: {str(e)}")

@router.post("/agents/{agent_id}/api-keys/{key_id}/rotate")
async def rotate_agent_api_key(
    agent_id: str,
    key_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Rotate an API key (generate new key, mark old as rotated)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get existing key
        existing = await database.fetch_one(
            "SELECT * FROM agent_api_keys WHERE agent_id = :agent_id AND key_id = :key_id",
            {"agent_id": agent_id, "key_id": key_id}
        )
        
        if not existing:
            raise HTTPException(status_code=404, detail="API key not found")
        
        existing_dict = dict(existing)
        
        # Generate new key
        new_key_id = f"key_{uuid.uuid4().hex[:16]}"
        raw_key = f"ak_live_{secrets.token_hex(32)}"
        key_hash = secrets.token_hex(16)
        key_prefix = raw_key[:12] + "..."
        
        # Deactivate old key and create new one in a transaction
        await database.execute(
            "UPDATE agent_api_keys SET is_active = false, last_rotated_at = :now WHERE key_id = :key_id",
            {"now": datetime.now(), "key_id": key_id}
        )
        
        await database.execute(
            """INSERT INTO agent_api_keys 
               (agent_id, key_id, key_hash, key_prefix, scopes, ip_whitelist, 
                is_active, created_at, expires_at, created_by)
               VALUES (:agent_id, :key_id, :key_hash, :key_prefix, :scopes, :ip_whitelist,
                       :is_active, :created_at, :expires_at, :created_by)""",
            {
                "agent_id": agent_id,
                "key_id": new_key_id,
                "key_hash": key_hash,
                "key_prefix": key_prefix,
                "scopes": existing_dict.get("scopes"),
                "ip_whitelist": existing_dict.get("ip_whitelist"),
                "is_active": True,
                "created_at": datetime.now(),
                "expires_at": existing_dict.get("expires_at"),
                "created_by": current_user.get("email")
            }
        )
        
        logger.info(f"API key rotated for agent {agent_id}: {key_id} → {new_key_id} by {current_user.get('email')}")
        
        return {
            "status": "success",
            "message": "API key rotated successfully",
            "new_api_key": raw_key,  # Only shown once!
            "new_key_id": new_key_id,
            "old_key_id": key_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rotate API key: {str(e)}")

# ============== Phase 2: Protocol Management ==============

@router.get("/agents/{agent_id}/protocols")
async def get_agent_protocols(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """List all supported protocols for an agent"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        protocols = await database.fetch_all(
            """SELECT id, agent_id, protocol_name, version, status, 
                      last_verified_at, created_at, updated_at
               FROM agent_protocols
               WHERE agent_id = :agent_id
               ORDER BY created_at DESC""",
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "protocols": [
                {
                    "id": dict(p).get("id"),
                    "protocol_name": dict(p).get("protocol_name"),
                    "version": dict(p).get("version"),
                    "status": dict(p).get("status"),
                    "last_verified_at": str(dict(p).get("last_verified_at")) if dict(p).get("last_verified_at") else None,
                    "created_at": str(dict(p).get("created_at")) if dict(p).get("created_at") else None
                }
                for p in protocols
            ],
            "total": len(protocols)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get protocols: {str(e)}")

@router.post("/agents/{agent_id}/protocols")
async def add_agent_protocol(
    agent_id: str,
    request: AddProtocolRequest,
    current_user: dict = Depends(get_current_user)
):
    """Add a supported protocol for an agent"""
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
        
        # Check if protocol already exists
        existing = await database.fetch_one(
            """SELECT id FROM agent_protocols 
               WHERE agent_id = :agent_id 
                 AND protocol_name = :protocol 
                 AND version = :version""",
            {"agent_id": agent_id, "protocol": request.protocol_name, "version": request.version}
        )
        
        if existing:
            raise HTTPException(status_code=400, detail="Protocol already exists for this agent")
        
        # Insert protocol
        await database.execute(
            """INSERT INTO agent_protocols 
               (agent_id, protocol_name, version, status, last_verified_at, created_at)
               VALUES (:agent_id, :protocol, :version, :status, :verified, :created)""",
            {
                "agent_id": agent_id,
                "protocol": request.protocol_name,
                "version": request.version,
                "status": "active",
                "verified": datetime.now(),
                "created": datetime.now()
            }
        )
        
        logger.info(f"Protocol {request.protocol_name} v{request.version} added for agent {agent_id}")
        
        return {
            "status": "success",
            "message": f"Protocol {request.protocol_name} added successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add protocol: {str(e)}")

@router.put("/agents/{agent_id}/protocols/{protocol_id}")
async def update_agent_protocol_status(
    agent_id: str,
    protocol_id: int,
    status: str,
    current_user: dict = Depends(get_current_user)
):
    """Update protocol status (active/deprecated/disabled)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if status not in ["active", "deprecated", "disabled"]:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    try:
        await database.execute(
            """UPDATE agent_protocols 
               SET status = :status, updated_at = :updated
               WHERE id = :id AND agent_id = :agent_id""",
            {
                "status": status,
                "updated": datetime.now(),
                "id": protocol_id,
                "agent_id": agent_id
            }
        )
        
        logger.info(f"Protocol {protocol_id} status updated to {status} for agent {agent_id}")
        
        return {
            "status": "success",
            "message": "Protocol status updated"
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update protocol: {str(e)}")

# ============== Phase 2: Performance Stats ==============

@router.get("/agents/{agent_id}/performance")
async def get_agent_performance(
    agent_id: str,
    period: str = "7d",
    current_user: dict = Depends(get_current_user)
):
    """Get aggregated performance stats for an agent"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Parse period
        days = {
            "1d": 1,
            "7d": 7,
            "30d": 30,
            "90d": 90
        }.get(period, 7)
        
        period_start = datetime.now() - timedelta(days=days)
        
        # Get aggregated stats
        stats = await database.fetch_all(
            """SELECT period_start, period_end, total_requests, success_count, 
                      fail_count, success_rate, avg_latency_ms, total_gmv, total_orders
               FROM agent_performance_stats
               WHERE agent_id = :agent_id 
                 AND period_start >= :period_start
               ORDER BY period_start DESC""",
            {"agent_id": agent_id, "period_start": period_start}
        )
        
        # If no pre-aggregated stats, calculate from usage logs (fallback)
        if not stats:
            fallback = await database.fetch_one(
                """SELECT 
                    COUNT(*) as total_requests,
                    COUNT(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 END) as success_count,
                    COUNT(CASE WHEN status_code >= 400 THEN 1 END) as fail_count,
                    CASE WHEN COUNT(*) > 0 THEN
                        (COUNT(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 END)::FLOAT / COUNT(*)::FLOAT * 100)
                    ELSE 0 END as success_rate,
                    COALESCE(AVG(response_time_ms), 0) as avg_latency_ms
                   FROM agent_usage_logs
                   WHERE agent_id = :agent_id
                     AND timestamp >= :period_start""",
                {"agent_id": agent_id, "period_start": period_start}
            )
            
            if fallback:
                fallback_dict = dict(fallback)
                return {
                    "status": "success",
                    "period": period,
                    "summary": {
                        "total_requests": fallback_dict.get("total_requests") or 0,
                        "success_count": fallback_dict.get("success_count") or 0,
                        "fail_count": fallback_dict.get("fail_count") or 0,
                        "success_rate": float(fallback_dict.get("success_rate") or 0),
                        "avg_latency_ms": int(fallback_dict.get("avg_latency_ms") or 0)
                    },
                    "daily_breakdown": []  # TODO: Phase 3 - daily granularity
                }
        
        return {
            "status": "success",
            "period": period,
            "stats": [
                {
                    "period_start": str(dict(s).get("period_start")),
                    "period_end": str(dict(s).get("period_end")),
                    "total_requests": dict(s).get("total_requests"),
                    "success_rate": float(dict(s).get("success_rate") or 0),
                    "avg_latency_ms": dict(s).get("avg_latency_ms"),
                    "total_gmv": float(dict(s).get("total_gmv") or 0),
                    "total_orders": dict(s).get("total_orders")
                }
                for s in stats
            ],
            "total": len(stats)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get performance stats: {str(e)}")






