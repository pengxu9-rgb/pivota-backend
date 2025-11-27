"""
Agent Account Management - REBUILT from scratch
Simplified and adapted to actual database schema
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, validator
from typing import Optional
from datetime import datetime, timedelta
import secrets
import hashlib

from db.database import database
from utils.auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/agent/account", tags=["agent-account"])

# ============================================================================
# Models
# ============================================================================

class AgentRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    agent_name: str
    company: Optional[str] = None
    description: Optional[str] = None
    phone: Optional[str] = None
    
    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        return v

class AgentLoginRequest(BaseModel):
    email: EmailStr
    password: str

class AgentInfo(BaseModel):
    agent_id: str
    agent_name: str
    email: str
    company: str
    description: str
    status: str
    # [Phase 6.2] Agent tier (basic/premium)
    agent_type: str = "basic"

class AgentLoginResponse(BaseModel):
    success: bool
    token: str
    agent: AgentInfo
    api_key: str

# ============================================================================
# Endpoints
# ============================================================================

@router.post("/register")
async def register_agent(data: AgentRegisterRequest):
    """
    Register a new AI Agent
    Simplified to work with actual database schema
    """
    try:
        # 1. Check if user already exists
        existing_user = await database.fetch_one(
            "SELECT id FROM users WHERE email = :email",
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
        api_key_raw = secrets.token_bytes(32)
        api_key = f"ak_live_{api_key_raw.hex()}"
        api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        
        # 4. Create agent record - aligned with current agents table schema
        await database.execute(
            """
            INSERT INTO agents (
                agent_id,
                agent_name,
                owner_email,
                description,
                api_key,
                api_key_hash,
                is_active,
                created_at,
                updated_at
            )
            VALUES (
                :agent_id,
                :agent_name,
                :owner_email,
                :description,
                :api_key,
                :api_key_hash,
                TRUE,
                NOW(),
                NOW()
            )
            """,
            {
                "agent_id": agent_id,
                "agent_name": data.agent_name,
                "owner_email": data.email,
                "description": data.description or "",
                "api_key": api_key,
                "api_key_hash": api_key_hash,
            }
        )
        
        print(f"✅ Agent created: {agent_id} for {data.email}")
        
        return {
            "success": True,
            "message": "Agent account created successfully",
            "agent_id": agent_id,
            "api_key": api_key,
            "email": data.email,
            "important": "Save your API key now! It won't be shown again."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        # If agent creation failed, clean up user account
        try:
            await database.execute(
                "DELETE FROM users WHERE email = :email AND role = 'agent'",
                {"email": data.email}
            )
            print(f"🧹 Cleaned up user account after failure")
        except:
            pass
        
        import traceback
        print(f"❌ Agent registration error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Registration failed: {str(e)}"
        )


@router.post("/login", response_model=AgentLoginResponse)
async def login_agent(data: AgentLoginRequest):
    """Agent login"""
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
        
        # 3. Get agent record
        agent = await database.fetch_one(
            """
            SELECT agent_id, agent_name, owner_email, api_key, agent_type
            FROM agents
            WHERE owner_email = :email
            """,
            {"email": data.email}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent record not found")
        
        # databases.Record → plain dict for safe .get() usage
        agent_dict = dict(agent)
        
        # 4. Update last login
        await database.execute(
            "UPDATE users SET last_login = :last_login WHERE email = :email",
            {"last_login": datetime.utcnow(), "email": data.email}
        )
        
        # 5. Create JWT token
        # Extend agent portal session to reduce unexpected logouts.
        # Other roles keep the default (24h) expiry from utils.auth.
        token = create_access_token(
            {
                "sub": user["email"],
                "email": user["email"],  # Required by get_current_user
                "user_id": str(user["id"]),
                "role": "agent",
                "agent_id": agent["agent_id"],
            },
            # Agent portal sessions: ~7 days
            expires_delta=timedelta(days=7),
        )
        
        # Get agent name safely
        agent_name = "Agent"  # Default
        try:
            if agent["agent_name"]:
                agent_name = agent["agent_name"]
        except (KeyError, TypeError):
            # Fallback to user's full name if available
            try:
                if user["full_name"]:
                    agent_name = user["full_name"]
            except (KeyError, TypeError):
                pass
        
        return AgentLoginResponse(
            success=True,
            token=token,
            agent={
                "agent_id": agent_dict["agent_id"],
                "agent_name": agent_name,
                "email": agent_dict["owner_email"],
                "company": "",
                "description": "",
                "status": "active",
                "agent_type": agent_dict.get("agent_type") or "basic",
            },
            api_key=agent_dict["api_key"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )
