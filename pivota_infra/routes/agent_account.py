"""
Agent Account Management
Registration and login system for AI Agents
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, validator
from typing import Optional
from datetime import datetime
import secrets
import hashlib

from db.database import database
from utils.auth import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/agent/account", tags=["agent-account"])


# ============================================================================
# Request/Response Models
# ============================================================================

class AgentRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    agent_name: str
    company: Optional[str] = None
    
    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one digit')
        return v

class AgentLoginRequest(BaseModel):
    email: EmailStr
    password: str

class AgentLoginResponse(BaseModel):
    success: bool
    token: str
    agent: dict
    api_key: str


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/register")
async def register_agent(data: AgentRegisterRequest):
    """
    Register a new AI Agent account
    Creates both user account and agent record with API key
    """
    try:
        # 1. Check if user already exists
        existing_user = await database.fetch_one(
            "SELECT user_id FROM users WHERE email = :email",
            {"email": data.email}
        )
        
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # 2. Create user account
        password_hash = hash_password(data.password)
        
        await database.execute(
            """
            INSERT INTO users (email, password_hash, full_name, role, active)
            VALUES (:email, :password_hash, :full_name, :role, :active)
            """,
            {
                "email": data.email,
                "password_hash": password_hash,
                "full_name": data.agent_name,
                "role": "agent",
                "active": True
            }
        )
        
        # 3. Generate agent_id and API key
        agent_id = f"agent_{secrets.token_hex(8)}"
        
        # Generate API key: ak_live_<64 hex chars>
        api_key_raw = secrets.token_bytes(32)
        api_key = f"ak_live_{api_key_raw.hex()}"
        
        # Hash the API key for storage (store hash, not plaintext)
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        # 4. Create agent record using the actual table structure
        await database.execute(
            """
            INSERT INTO agents (
                agent_id, agent_name, agent_type, description,
                api_key, api_key_hash, is_active,
                owner_email, rate_limit, daily_quota,
                created_at
            )
            VALUES (
                :agent_id, :agent_name, :agent_type, :description,
                :api_key, :api_key_hash, :is_active,
                :owner_email, :rate_limit, :daily_quota,
                :created_at
            )
            """,
            {
                "agent_id": agent_id,
                "agent_name": data.agent_name,
                "agent_type": "custom",
                "description": data.description,
                "api_key": api_key,
                "api_key_hash": api_key_hash,
                "is_active": True,
                "owner_email": data.email,
                "rate_limit": 60,  # 60 requests per minute
                "daily_quota": 1000,  # 1000 requests per day
                "created_at": datetime.utcnow()
            }
        )
        
        return {
            "success": True,
            "message": "Agent account created successfully",
            "agent_id": agent_id,
            "api_key": api_key,  # Return only once - save this!
            "email": data.email,
            "important": "Save your API key now! It won't be shown again."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Registration failed: {str(e)}"
        )


@router.post("/login", response_model=AgentLoginResponse)
async def login_agent(data: AgentLoginRequest):
    """
    Login for AI Agent
    Returns JWT token and API key
    """
    try:
        # 1. Find user
        user = await database.fetch_one(
            "SELECT id, email, password_hash, full_name, role, active FROM users WHERE email = :email",
            {"email": data.email}
        )
        
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        if user["role"] != "agent":
            raise HTTPException(status_code=403, detail="This login is for agents only")
        
        if not user["active"]:
            raise HTTPException(status_code=403, detail="Account is deactivated")
        
        # 2. Verify password
        if not verify_password(data.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        # 3. Get agent record using actual column names
        agent = await database.fetch_one(
            "SELECT agent_id, agent_name, owner_email, description, is_active, allowed_merchants, api_key FROM agents WHERE owner_email = :email",
            {"email": data.email}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent record not found. Please contact support.")
        
        # 4. Use the API key from agents table (already stored there)
        api_key = agent["api_key"]
        
        # 5. Update last login
        await database.execute(
            "UPDATE users SET last_login = :last_login WHERE email = :email",
            {"last_login": datetime.utcnow(), "email": data.email}
        )
        
        # 6. Create JWT token
        token = create_access_token({
            "sub": user["email"],
            "user_id": str(user["id"]),
            "role": "agent",
            "agent_id": agent["agent_id"]
        })
        
        return AgentLoginResponse(
            success=True,
            token=token,
            agent={
                "agent_id": agent["agent_id"],
                "agent_name": agent["agent_name"],
                "email": agent["owner_email"],
                "company": "",  # Not stored in agent table
                "description": agent.get("description", ""),
                "status": "active" if agent["is_active"] else "inactive"
            },
            api_key=api_key
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )


@router.get("/me")
async def get_agent_profile(current_user: dict = Depends(lambda: {})):
    """
    Get current agent profile
    Requires authentication token
    """
    from utils.auth import get_current_user
    user = await get_current_user()
    
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="Not an agent account")
    
    # Get agent details
    agent = await database.fetch_one(
        """
        SELECT agent_id, name, email, company, status, tier, 
               rate_limit_rpm, daily_quota, total_orders, total_gmv
        FROM agents 
        WHERE email = :email
        """,
        {"email": user["email"]}
    )
    
    if not agent:
        raise HTTPException(status_code=404, detail="Agent record not found")
    
    return {
        "success": True,
        "agent": dict(agent)
    }


